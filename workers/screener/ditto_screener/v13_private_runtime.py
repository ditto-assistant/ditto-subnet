"""Fresh rootless V13 case sessions with a drained provider-side ledger.

The resolver must read signed image URLs and exact identities from current
Platform replay/control state. This module never accepts miner-provided image
URLs or protected case bytes. A missing resolver leaves execution unavailable.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ditto_screener.config import ScreenerConfig
from ditto_screener.conversation_runtime import ConversationRuntime
from ditto_screener.v13_private_adapter import (
    FreshCaseSession,
    PrivateExecutionUnavailable,
    SettledCaseLedger,
)

MAX_PRIVATE_REPLAY_COST_MICROUSD = 20_000_000


@dataclass(frozen=True)
class TrustedPrivateImage:
    """Short-lived image input returned by a trusted Platform resolver."""

    role: Literal["target", "known_benign"]
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str
    image_sha256: str
    image_id: str
    size_bytes: int
    url: str


class TrustedPrivateImageResolver(Protocol):
    """Fetch current replay/control bindings directly from Platform."""

    async def resolve(
        self,
        *,
        role: Literal["target", "known_benign"],
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        image_sha256: str,
    ) -> TrustedPrivateImage: ...


@dataclass(frozen=True)
class _ImageLaunch:
    assessment_id: UUID
    screened_image_url: str
    screened_image_size_bytes: int
    screened_image_sha256: str
    screened_image_id: str


class _PrivateBrokerUsage(BaseModel):
    """Terminal sidecar state; ordinary incomplete usage cannot pass."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    settled: Literal[True]
    requests: int = Field(ge=0, le=300)
    chat_dispatches: int = Field(ge=0, le=300)
    successful_chat_responses: int = Field(ge=0, le=300)
    unmetered: bool
    failed: bool
    failure: dict[str, object] | None = None


class _ConversationPrivateCaseSession(FreshCaseSession):
    def __init__(
        self,
        runtime: ConversationRuntime,
        *,
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        image_sha256: str,
        session_id: str,
        case_id: str,
    ) -> None:
        self.runtime = runtime
        self.agent_id = agent_id
        self.attempt_id = attempt_id
        self.artifact_sha256 = artifact_sha256
        self.image_sha256 = image_sha256
        self.session_id = session_id
        self.case_id = case_id
        self._closed = False

    async def _post(
        self, path: str, payload: Mapping[str, object], timeout: float
    ) -> dict[str, object]:
        async with httpx.AsyncClient(
            trust_env=False,
            transport=self.runtime.transport(timeout=max(1, min(120, int(timeout)))),
            timeout=timeout,
        ) as client:
            response = await client.post("http://127.0.0.1" + path, json=payload)
            response.raise_for_status()
            body = response.json()
            if type(body) is not dict:
                raise PrivateExecutionUnavailable("private case response unavailable")
            return body

    async def seed(self, payload: Mapping[str, object], timeout: float) -> None:
        await self._post("/seed", payload, timeout)

    async def run(
        self, payload: Mapping[str, object], timeout: float
    ) -> dict[str, object]:
        return await self._post("/run", payload, timeout)

    async def settle(self) -> SettledCaseLedger:
        if self._closed:
            raise PrivateExecutionUnavailable("private case already settled")
        raw = await self.runtime.stop_private()
        self._closed = True
        usage = _PrivateBrokerUsage.model_validate(raw)
        if (
            usage.successful_chat_responses > usage.chat_dispatches
            or usage.chat_dispatches > usage.requests
        ):
            raise PrivateExecutionUnavailable("private broker ledger invalid")
        status: Literal[
            "attributed", "zero_call", "no_successful_response", "unattributed"
        ]
        if usage.chat_dispatches == 0:
            status = "zero_call"
        elif usage.successful_chat_responses == 0:
            status = "no_successful_response"
        elif (
            usage.failed
            or usage.unmetered
            or usage.successful_chat_responses != usage.chat_dispatches
        ):
            status = "unattributed"
        else:
            status = "attributed"
        failure_code = (usage.failure or {}).get("code")
        return SettledCaseLedger(
            session_sha256=hashlib.sha256(self.session_id.encode()).hexdigest(),
            case_sha256=hmac.new(
                self.session_id.encode(), self.case_id.encode(), hashlib.sha256
            ).hexdigest(),
            agent_id=self.agent_id,
            attempt_id=self.attempt_id,
            artifact_sha256=self.artifact_sha256,
            image_sha256=self.image_sha256,
            status=status,
            chat_dispatches=usage.chat_dispatches,
            successful_responses=usage.successful_chat_responses,
            attributed_responses=(
                usage.successful_chat_responses if status == "attributed" else 0
            ),
            unattributed=(0 if status == "attributed" else usage.chat_dispatches),
            unreadable_requests=int(
                failure_code in {"invalid_json", "invalid_body", "truncated_request"}
            ),
            truncated=failure_code
            in {"oversized_provider_response", "oversized_harness_response"},
            cross_case_starts=0,
            cross_case_claims=0,
        )

    async def close(self) -> None:
        if not self._closed:
            await self.runtime.stop_private()
            self._closed = True


class ConversationPrivateCaseSessionFactory:
    """One fresh verified image, network, container, and relay per case."""

    def __init__(
        self,
        *,
        config: ScreenerConfig,
        provider_key: str,
        resolver: TrustedPrivateImageResolver | None,
        max_cost_microusd: int,
    ) -> None:
        if (
            type(max_cost_microusd) is not int
            or not 0 < max_cost_microusd <= MAX_PRIVATE_REPLAY_COST_MICROUSD
        ):
            raise PrivateExecutionUnavailable("private aggregate budget invalid")
        self.config = config
        self.provider_key = provider_key
        self.resolver = resolver
        self.max_cost_microusd = max_cost_microusd
        self._case_budget_microusd: int | None = None
        self._max_cases = 0
        self._cases_started = 0

    def configure_budget(self, pair_count: int) -> None:
        """Partition the hard total cap before any fresh sidecar can dispatch.

        Two sides on each of two pinned images open exactly four sessions per
        pair. Every sidecar enforces its slice before provider dispatch, so even
        cancellation, failed usage reads, and unspent slices cannot exceed the
        total cap. A second configuration or extra case fails closed.
        """
        if (
            self._case_budget_microusd is not None
            or type(pair_count) is not int
            or not 60 <= pair_count <= 512
        ):
            raise PrivateExecutionUnavailable("private aggregate budget unavailable")
        max_cases = 4 * pair_count
        case_budget = self.max_cost_microusd // max_cases
        if case_budget < 1:
            raise PrivateExecutionUnavailable("private aggregate budget unavailable")
        self._max_cases = max_cases
        self._case_budget_microusd = case_budget

    async def open_case(
        self,
        *,
        role: Literal["target", "known_benign"],
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        image_sha256: str,
        session_id: str,
        case_id: str,
    ) -> FreshCaseSession:
        if (
            self.resolver is None
            or not self.provider_key
            or self._case_budget_microusd is None
            or self._cases_started >= self._max_cases
        ):
            raise PrivateExecutionUnavailable("trusted private route unavailable")
        # Burn a slice before any await. A failed setup never grants a second
        # chance to spend the same slice against another sidecar.
        self._cases_started += 1
        image = await self.resolver.resolve(
            role=role,
            agent_id=agent_id,
            attempt_id=attempt_id,
            artifact_sha256=artifact_sha256,
            image_sha256=image_sha256,
        )
        if (
            image.role != role
            or image.agent_id != agent_id
            or image.attempt_id != attempt_id
            or image.artifact_sha256 != artifact_sha256
            or image.image_sha256 != image_sha256
            or not image.image_id.startswith("sha256:")
            or len(image.image_id) != 71
            or not 0 < image.size_bytes <= 8 << 30
        ):
            raise PrivateExecutionUnavailable("private image binding mismatch")
        runtime = ConversationRuntime(
            self.config,
            _ImageLaunch(
                assessment_id=uuid4(),
                screened_image_url=image.url,
                screened_image_size_bytes=image.size_bytes,
                screened_image_sha256=image.image_sha256,
                screened_image_id=image.image_id,
            ),
            self.provider_key,
            private_case=True,
            private_budget_microusd=self._case_budget_microusd,
        )
        try:
            await runtime.start()
        except BaseException:
            await runtime.stop()
            raise PrivateExecutionUnavailable(
                "private sandbox startup unavailable"
            ) from None
        return _ConversationPrivateCaseSession(
            runtime,
            agent_id=agent_id,
            attempt_id=attempt_id,
            artifact_sha256=artifact_sha256,
            image_sha256=image_sha256,
            session_id=session_id,
            case_id=case_id,
        )
