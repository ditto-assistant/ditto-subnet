"""Async client for the hosted dittobench-api scoring engine.

Drives the run_size pipeline over HTTP: ``POST /v1/submit`` with the platform's
presigned ``tarball_url`` (mode B), then polls
``GET /v1/runs/{id}`` until the job is ``done`` and parses the ``ScoreReport``.

The returned report is the platform :class:`ScoreReport` shape (the dittobench
wire contract is identical by design), so it round-trips straight back into
``POST /validator/agent/{id}/score``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx

from ditto.api_models.benchmark_progress import (
    MAX_BENCHMARK_CHECKS,
    BenchmarkProgressStage,
)
from ditto.api_models.validator import ScoreReport
from ditto.api_models.validator_capabilities import (
    InferenceCalibrationRoute,
    ScorerBenchmarkCapability,
    V7InferenceCalibration,
    ValidatorStackIdentity,
)
from ditto.validator.errors import (
    DittobenchError,
    SandboxOomError,
    ValidatorInfrastructureError,
)

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

# Terminal job states reported by dittobench-api's store.
_DONE = "done"
_FAILED = "failed"

_PROGRESS_STAGE_BY_STATUS: dict[str, BenchmarkProgressStage] = {
    "queued": "preparing",
    "building": "building_harness",
    "generating": "generating_dataset",
    "seeding": "starting_harness",
    "running": "running_benchmark",
    "scoring": "finalizing",
    "done": "finalizing",
    "failed": "failed_retrying",
}
_STABLE_COUNT_STATUSES = {"running", "scoring", "done"}
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SOFTWARE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,63}$")

# Benchmark versions this validator knows how to drive. The allowlist is
# fail-closed: an advertisement or ticket naming anything outside it is refused
# rather than scored blind. Versions >= 3 share one scorer contract (/v2/score,
# pinned dataset, policy-9 screened image), so version-specific behaviour is
# expressed as ``>= _SCREENED_IMAGE_BENCH_VERSION`` rather than per-version arms.
_SUPPORTED_BENCH_VERSIONS = (2, 3, 4, 5, 6, 7)
_SCREENED_IMAGE_BENCH_VERSION = 3


def _parse_v7_calibration(payload: object) -> V7InferenceCalibration | None:
    """Parse the scorer's exact reviewed manifest identity, or fail closed."""
    if not isinstance(payload, dict):
        return None
    raw_routes = payload.get("supported_routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        return None
    try:
        routes = tuple(
            sorted(
                (
                    InferenceCalibrationRoute.model_validate(route)
                    for route in raw_routes
                ),
                key=lambda route: (
                    route.provider,
                    route.profile_revision,
                    route.model,
                ),
            )
        )
        return V7InferenceCalibration.model_validate(
            {**payload, "supported_routes": routes}
        )
    except ValueError:
        return None


def _is_embedding_infrastructure_failure(error: str) -> bool:
    """Identify legacy scorer failures from a validator-owned embedding route.

    Kept only for the bounded mixed-version rollout. New scorers emit the
    structured ``embedding_provider_unavailable`` failure code below.
    """
    normalized = error.lower()
    return (
        "host.docker.internal:11434" in normalized
        or "ollama embed request" in normalized
        or ("ollama" in normalized and "embedding" in normalized)
        or "embedding service unavailable" in normalized
        or "embedding provider timed out" in normalized
    )


_SANDBOX_INFRASTRUCTURE_CODES = {
    "sandbox_oom",
    "sandbox_tmpfs_exhausted",
    # The scorer could not start the miner container because the validator's own
    # sandbox egress network was missing. This is validator infrastructure, not
    # the agent's fault, so it must end the sweep and back off rather than blame
    # and re-lease the agent in a tight resubmit loop.
    "sandbox_network_unavailable",
    # The scorer's locked-model-relay preflight failed (relay unreachable or its
    # upstream provider degraded mid-run). Also validator-side infrastructure, so
    # back off instead of failing the agent and re-leasing it in a loop.
    "model_relay_unavailable",
    # The platform-owned hosted embedding route failed or could not prove
    # complete delivery. The scorer fails closed and the validator retries the
    # whole ticket later; the agent must never receive a score for this attempt.
    "embedding_provider_unavailable",
}


def _sandbox_infrastructure_failure_code(payload: dict[str, object]) -> str | None:
    """Accept only the scorer's narrow, source-free resource classifier."""
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return None
    if (
        failure.get("kind") != "validator_infrastructure"
        or failure.get("retryable") is not True
    ):
        return None
    code = failure.get("code")
    return (
        code
        if isinstance(code, str) and code in _SANDBOX_INFRASTRUCTURE_CODES
        else None
    )


@dataclass(frozen=True)
class DittobenchProgressSnapshot:
    """Allowlisted progress extracted from an otherwise private scorer job."""

    stage: BenchmarkProgressStage
    completed: int | None = None
    total: int | None = None
    # Opaque per-run identity, derived from the dittobench run id. Set by the
    # poll loop once the run exists so the worker can tell a fresh re-attempt
    # apart from the same still-live lease. Absent (None) means unknown.
    run_token: str | None = None


@dataclass(frozen=True)
class InferenceBrokerSession:
    session_id: str
    activation_secret: str
    broker_public_key: str


ProgressCallback = Callable[[DittobenchProgressSnapshot], Awaitable[None]]


def safe_progress_snapshot(payload: object) -> DittobenchProgressSnapshot | None:
    """Extract only status and aggregate counts from a DittoBench poll response.

    Pre-running totals can change while the generated suite is assembled, so
    counts remain unknown until the raw scorer reaches ``running``. Malformed
    counts degrade to unknown without affecting the benchmark.
    """
    if not isinstance(payload, dict):
        return None
    raw_status = payload.get("status")
    if not isinstance(raw_status, str):
        return None
    stage = _PROGRESS_STAGE_BY_STATUS.get(raw_status)
    if stage is None or raw_status not in _STABLE_COUNT_STATUSES:
        return None if stage is None else DittobenchProgressSnapshot(stage=stage)

    raw_progress = payload.get("progress")
    if not isinstance(raw_progress, dict):
        return DittobenchProgressSnapshot(stage=stage)
    completed = raw_progress.get("done")
    total = raw_progress.get("total")
    if (
        type(completed) is not int
        or type(total) is not int
        or completed < 0
        or total < 1
        or completed > total
        or total > MAX_BENCHMARK_CHECKS
    ):
        return DittobenchProgressSnapshot(stage=stage)
    return DittobenchProgressSnapshot(stage=stage, completed=completed, total=total)


class DittobenchClient:
    """HTTP client for one dittobench-api base URL."""

    def __init__(self, config: ValidatorConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        # Raw, opaque ``details`` blob from the most recent scored run (bench
        # version, paraphrase/injection telemetry, token totals). Not part of the
        # signed/DB ScoreReport contract — captured here only so the validator can
        # surface it in aggregate W&B telemetry.
        self.last_details: dict[str, object] = {}
        # Verified transcript bytes are keyed by immutable run id. Multi-seed
        # confirmation may select a representative that was not evaluated last,
        # so one mutable slot is insufficient. Keep a small insertion-ordered
        # cache; publication consumes the selected entry.
        self._transcripts: dict[str, bytes] = {}
        # Backward-compatible diagnostic view of the most recent run.
        self.last_transcript: bytes | None = None
        # Legacy scorers omit this field and therefore remain safely capacity=1.
        self.full_run_capacity = (
            int(getattr(config, "benchmark_capacity", 1))
            if getattr(config, "dittobench_mock", False)
            else 1
        )

    def take_transcript(self, run_id: str) -> bytes | None:
        """Consume the verified transcript belonging to exactly ``run_id``."""
        return self._transcripts.pop(run_id, None)

    async def prepare_inference_session(self) -> InferenceBrokerSession:
        """Create a trusted memory-only broker key before claiming provider access."""
        try:
            response = await self._client.post(
                f"{self._config.dittobench_api_url}/v1/inference/session"
            )
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"inference broker preparation failed: {error}"
            ) from error
        if response.status_code != 201:
            raise ValidatorInfrastructureError("inference broker preparation rejected")
        try:
            body = response.json()
            return InferenceBrokerSession(
                session_id=str(body["session_id"]),
                activation_secret=str(body["activation_secret"]),
                broker_public_key=str(body["broker_public_key"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValidatorInfrastructureError(
                "inference broker returned an invalid session"
            ) from error

    async def activate_inference_session(
        self,
        session: InferenceBrokerSession,
        *,
        grant_id: UUID,
        agent_id: UUID,
        slot_id: str,
        ticket_deadline: datetime,
        bearer: str,
        proxy_url: str,
        generation: int,
        expires_at: datetime,
        provider: str | None,
        profile_revision: str | None,
        model: str | None,
    ) -> None:
        """Deliver the platform capability directly to the trusted broker."""
        try:
            activation: dict[str, object] = {
                "activation_secret": session.activation_secret,
                "grant_id": str(grant_id),
                "agent_id": str(agent_id),
                "slot_id": slot_id,
                "ticket_deadline": ticket_deadline.isoformat(),
                "bearer": bearer,
                "proxy_url": proxy_url,
                "generation": generation,
                "expires_at": expires_at.isoformat(),
            }
            if provider is not None:
                activation["provider"] = provider
            if profile_revision is not None:
                activation["profile_revision"] = profile_revision
            if model is not None:
                activation["model"] = model
            response = await self._client.post(
                f"{self._config.dittobench_api_url}/v1/inference/session/"
                f"{session.session_id}/activate",
                json=activation,
            )
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"inference broker activation failed: {error}"
            ) from error
        if response.status_code != 200:
            raise ValidatorInfrastructureError("inference broker activation rejected")

    async def cancel_inference_session(self, session_id: str) -> None:
        """Best-effort deletion for pre-run failures and completed sessions."""
        with contextlib.suppress(httpx.HTTPError):
            await self._client.delete(
                f"{self._config.dittobench_api_url}/v1/inference/session/{session_id}"
            )

    async def scorer_benchmark_capability(
        self, stack: ValidatorStackIdentity
    ) -> ScorerBenchmarkCapability:
        """Observe scorer support and bind post-v2 claims to signed stack identity.

        Legacy 404s, malformed replies, timeouts, and source mismatches all fail
        closed to v2. A newer scorer may advertise future benchmark versions;
        report only the intersection this validator knows how to drive so a
        forward-compatible rollout stays healthy without claiming unsupported
        work. Unknown versions at or below this validator's current maximum are
        still malformed and fail closed.
        """
        legacy = ScorerBenchmarkCapability(
            status="legacy_v2", supported_bench_versions=(2,)
        )
        if self._config.dittobench_mock:
            return legacy
        self.full_run_capacity = 1
        try:
            response = await self._client.get(
                f"{self._config.dittobench_api_url}/v1/capabilities",
                timeout=getattr(
                    self._config, "dittobench_capabilities_timeout_seconds", 3.0
                ),
            )
        except httpx.HTTPError:
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )
        if response.status_code == 404:
            return legacy
        if response.status_code != 200:
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )
        software_version = payload.get("software_version")
        source_revision = payload.get("source_revision")
        versions = payload.get("supported_bench_versions")
        full_run_capacity = payload.get("full_run_capacity", 1)
        if (
            not isinstance(software_version, str)
            or _SOFTWARE_VERSION.fullmatch(software_version) is None
            or not isinstance(source_revision, str)
            or _SOURCE_REVISION.fullmatch(source_revision) is None
            or not isinstance(versions, list)
            or not versions
            or any(type(version) is not int for version in versions)
            or type(full_run_capacity) is not int
            or not 1 <= full_run_capacity <= 8
        ):
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )
        advertised_versions = tuple(sorted(set(versions)))
        if any(
            version not in _SUPPORTED_BENCH_VERSIONS
            and version <= _SUPPORTED_BENCH_VERSIONS[-1]
            for version in advertised_versions
        ):
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )
        observed_versions = tuple(
            version
            for version in advertised_versions
            if version in _SUPPORTED_BENCH_VERSIONS
        )
        if not observed_versions:
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )
        v7_calibration = _parse_v7_calibration(payload.get("v7_calibration"))
        if 7 in observed_versions and v7_calibration is None:
            observed_versions = tuple(
                version for version in observed_versions if version != 7
            )
            if not observed_versions:
                return ScorerBenchmarkCapability(
                    status="unreachable", supported_bench_versions=(2,)
                )
        elif 7 not in observed_versions:
            v7_calibration = None
        expected_revision = stack.components.dittobench_api.source_revision
        if source_revision != expected_revision:
            return ScorerBenchmarkCapability(
                status="identity_mismatch",
                supported_bench_versions=(2,),
                observed_at=int(time.time()),
                software_version=software_version,
                source_revision=source_revision,
            )
        self.full_run_capacity = full_run_capacity
        try:
            return ScorerBenchmarkCapability(
                status="fresh_verified",
                supported_bench_versions=observed_versions,
                observed_at=int(time.time()),
                software_version=software_version,
                source_revision=source_revision,
                v7_calibration=v7_calibration,
            )
        except ValueError:
            return ScorerBenchmarkCapability(
                status="unreachable", supported_bench_versions=(2,)
            )

    async def preflight(self) -> None:
        """Verify the functional embedding route before claiming a ticket.

        The configured URL is the sandbox-docker forwarder's port 11434, the
        same listener reached as ``host.docker.internal:11434`` by inner miner
        harnesses. A real embedding request checks the forwarder, Ollama, and
        the loaded model rather than merely checking container liveness.
        """
        if self._config.dittobench_mock:
            return
        try:
            response = await self._client.post(
                self._config.embed_preflight_url,
                json={"model": "embeddinggemma", "input": "validator preflight"},
                timeout=self._config.embed_preflight_timeout_seconds,
            )
        except httpx.TimeoutException as e:
            raise ValidatorInfrastructureError(
                "embedding preflight timed out through the harness forwarder"
            ) from e
        except httpx.HTTPError as e:
            raise ValidatorInfrastructureError(
                f"embedding preflight could not reach the harness forwarder: {e}"
            ) from e
        if response.status_code != 200:
            raise ValidatorInfrastructureError(
                "embedding preflight through the harness forwarder rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            embeddings = response.json().get("embeddings")
        except (ValueError, AttributeError) as e:
            raise ValidatorInfrastructureError(
                "embedding preflight returned an invalid response"
            ) from e
        if (
            not isinstance(embeddings, list)
            or not embeddings
            or not isinstance(embeddings[0], list)
            or not embeddings[0]
        ):
            raise ValidatorInfrastructureError(
                "embedding preflight returned no embedding vector"
            )
        # The locked model is now ticket-scoped, so it cannot be probed before
        # the platform issues and the local broker activates a capability. Each
        # scored run performs that trusted probe after activation instead.

    async def _relay_preflight(self) -> None:
        """Verify the scorer's model-relay path from where the scorer runs it.

        The scorer reaches the locked model relay at ``host.docker.internal:11435``
        inside sandbox-docker's shared network namespace. This validator is NOT in
        that namespace — it reaches the relay only by service name — so it cannot
        itself detect a broken ``host.docker.internal`` mapping (e.g. a managed
        stack whose frozen compose predates the extra_hosts-on-sandbox-docker fix).
        The embed preflight above has the same blind spot. So a broken relay path
        stays invisible pre-lease, and the validator claims tickets it fast-fails
        at run start (``model_relay_unavailable``), burning each agent's retry
        budget until it wedges at quorum.

        Ask the scorer — which IS in the right namespace — to run its own relay
        health check via ``GET /v1/relay-preflight``. A broken relay surfaces as a
        503 ``validator_infrastructure`` envelope; raising here makes the sweep
        back off and claim no ticket, so a broken stack self-excludes and
        self-heals instead of wedging agents.

        Purely additive: an older scorer without the endpoint (404), or any other
        non-503 response or transport blip, is left to the existing per-run relay
        signal rather than newly blocking ticket claims.
        """
        try:
            response = await self._client.get(
                f"{self._config.dittobench_api_url}/v1/relay-preflight",
                timeout=self._config.embed_preflight_timeout_seconds,
            )
        except httpx.HTTPError:
            return
        if response.status_code != 503:
            return
        try:
            failure = response.json().get("failure")
        except ValueError:
            failure = None
        if not isinstance(failure, dict):
            return
        if failure.get("kind") == "validator_infrastructure":
            code = failure.get("code") or "model_relay_unavailable"
            raise ValidatorInfrastructureError(
                "scorer relay preflight reported the model relay is unreachable "
                f"from the scorer ({code}); not claiming a ticket this sweep"
            )

    async def score_tarball(
        self,
        *,
        tarball_url: str,
        tarball_sha256: str | None = None,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
        bench_version: int | None = None,
        progress_callback: ProgressCallback | None = None,
        screened_image_url: str | None = None,
        screened_image_sha256: str | None = None,
        screened_image_size_bytes: int | None = None,
        screened_image_id: str | None = None,
        screened_image_ref: str | None = None,
        inference_session_id: str | None = None,
        inference_grant_id: UUID | None = None,
        inference_agent_id: UUID | None = None,
        inference_slot_id: str | None = None,
        inference_ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        """Score a submission by its presigned tarball URL (mode B).

        Submits the scoring inputs, then polls until the run finishes.
        Raises :class:`DittobenchError` on a failed run or the overall timeout.

        ``tarball_sha256`` (the digest the platform registered at upload) is
        forwarded so the scorer re-verifies the fetched bytes against it and
        pins the Docker build tag to the content hash.

        ``seed`` pins the dataset seed. ``dataset_sha256`` selects the CANONICAL
        validator path: when set, this posts to dittobench-api **/v1/score** with
        the platform-pinned ``seed`` + ``dataset_sha256`` (+ ``run_size``), so the
        engine regenerates that exact dataset and FAILS the run on a hash mismatch
        (tamper-evidence — every k=3 validator provably scored the platform's
        dataset). Without ``dataset_sha256`` it uses /v1/submit (practice /
        version-bump re-score, fresh-or-CRN seed).
        """
        if bench_version is None or bench_version not in _SUPPORTED_BENCH_VERSIONS:
            raise DittobenchError(f"unsupported benchmark version {bench_version!r}")
        if self._config.dittobench_mock:
            self.last_details = {}
            self.last_transcript = None
            return self._mock_report()
        run_id = await self._submit(
            tarball_url=tarball_url,
            tarball_sha256=tarball_sha256,
            seed=seed,
            dataset_sha256=dataset_sha256,
            run_size=run_size,
            bench_version=bench_version,
            screened_image_url=screened_image_url,
            screened_image_sha256=screened_image_sha256,
            screened_image_size_bytes=screened_image_size_bytes,
            screened_image_id=screened_image_id,
            screened_image_ref=screened_image_ref,
            inference_session_id=inference_session_id,
            inference_grant_id=inference_grant_id,
            inference_agent_id=inference_agent_id,
            inference_slot_id=inference_slot_id,
            inference_ticket_deadline=inference_ticket_deadline,
        )
        return await self._poll(
            run_id,
            progress_callback=progress_callback,
            expected_bench_version=bench_version,
        )

    def _mock_report(self) -> ScoreReport:
        """Canned report for ``VALIDATOR_DITTOBENCH_MOCK`` (local plumbing tests)."""
        logger.info("dittobench mock enabled: returning canned ScoreReport")
        return ScoreReport(
            run_id=f"mock-{uuid4().hex[:12]}",
            seed=0,
            composite=0.9,
            tool_mean=0.9,
            memory_mean=0.9,
            median_ms=100,
            n=10,
            generated_at=datetime.now(UTC),
            per_case=[],
            structural_fingerprint=None,
            details=None,
        )

    async def _submit(
        self,
        *,
        tarball_url: str,
        tarball_sha256: str | None = None,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
        bench_version: int | None = None,
        screened_image_url: str | None = None,
        screened_image_sha256: str | None = None,
        screened_image_size_bytes: int | None = None,
        screened_image_id: str | None = None,
        screened_image_ref: str | None = None,
        inference_session_id: str | None = None,
        inference_grant_id: UUID | None = None,
        inference_agent_id: UUID | None = None,
        inference_slot_id: str | None = None,
        inference_ticket_deadline: datetime | None = None,
    ) -> str:
        if bench_version is None or bench_version not in _SUPPORTED_BENCH_VERSIONS:
            raise DittobenchError(f"unsupported benchmark version {bench_version!r}")
        body: dict[str, object] = {
            "tarball_url": tarball_url,
            "run_size": run_size or self._config.run_size,
        }
        if tarball_sha256:
            body["tarball_sha256"] = tarball_sha256
        screened_image_fields = (
            screened_image_url,
            screened_image_sha256,
            screened_image_size_bytes,
            screened_image_id,
            screened_image_ref,
        )
        if any(value is not None for value in screened_image_fields):
            if any(value is None for value in screened_image_fields):
                raise DittobenchError("screened image metadata must be complete")
            if not all(
                (
                    screened_image_url,
                    screened_image_sha256,
                    screened_image_id,
                    screened_image_ref,
                )
            ):
                raise DittobenchError("screened image identity fields cannot be empty")
            body.update(
                {
                    "screened_image_url": screened_image_url,
                    "screened_image_sha256": screened_image_sha256,
                    "screened_image_size_bytes": screened_image_size_bytes,
                    "screened_image_id": screened_image_id,
                    "screened_image_ref": screened_image_ref,
                }
            )
        elif bench_version >= _SCREENED_IMAGE_BENCH_VERSION:
            raise DittobenchError(
                f"benchmark v{bench_version} requires a verified screened image"
            )
        if seed is not None:
            body["seed"] = seed
        inference_identity = (
            inference_grant_id,
            inference_agent_id,
            inference_slot_id,
            inference_ticket_deadline,
        )
        if inference_session_id is not None:
            body["inference_session_id"] = inference_session_id
            if any(value is not None for value in inference_identity):
                if any(value is None for value in inference_identity):
                    raise DittobenchError("ticket inference identity must be complete")
                body.update(
                    {
                        "inference_grant_id": str(inference_grant_id),
                        "inference_agent_id": str(inference_agent_id),
                        "inference_slot_id": inference_slot_id,
                        "inference_ticket_deadline": (
                            inference_ticket_deadline.isoformat()
                            if inference_ticket_deadline is not None
                            else None
                        ),
                    }
                )
            elif bench_version >= 7:
                raise DittobenchError(
                    f"benchmark v{bench_version} requires ticket inference identity"
                )
        elif any(value is not None for value in inference_identity):
            raise DittobenchError(
                "ticket inference identity requires an inference session"
            )
        # Canonical validator path: pin the dataset so the engine fails on a
        # regenerated-hash mismatch. Otherwise the practice/re-score path.
        if bench_version >= _SCREENED_IMAGE_BENCH_VERSION:
            if not dataset_sha256:
                raise DittobenchError(
                    f"benchmark v{bench_version} requires a pinned dataset"
                )
            body["dataset_sha256"] = dataset_sha256
            body["bench_version"] = bench_version
            endpoint = "/v2/score"
        elif dataset_sha256:
            body["dataset_sha256"] = dataset_sha256
            endpoint = "/v1/score"
        else:
            endpoint = "/v1/submit"
        url = f"{self._config.dittobench_api_url}{endpoint}"
        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            raise DittobenchError(f"submit failed: {e}") from e
        if resp.status_code in (429, 503):
            raise ValidatorInfrastructureError(
                f"scorer admission unavailable ({resp.status_code})"
            )
        if resp.status_code not in (200, 202):
            raise DittobenchError(
                f"submit rejected ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        run_id = data.get("run_id")
        if not run_id:
            raise DittobenchError("submit response missing run_id")
        logger.info(
            "dittobench run %s started for %s",
            run_id,
            "screened image" if screened_image_url else "tarball build",
        )
        return str(run_id)

    async def _poll(
        self,
        run_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
        expected_bench_version: int | None = None,
    ) -> ScoreReport:
        if (
            expected_bench_version is None
            or expected_bench_version not in _SUPPORTED_BENCH_VERSIONS
        ):
            raise DittobenchError(
                f"unsupported benchmark version {expected_bench_version!r}"
            )
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        deadline = self._config.dittobench_timeout_seconds
        waited = 0.0
        # Opaque, stable-per-run token every progress heartbeat for this run
        # carries, so the platform can distinguish a fresh re-attempt of the same
        # lease from continued progress on the previous run and rebaseline the
        # monotonicity guard accordingly.
        run_token = hashlib.sha256(run_id.encode()).hexdigest()[:16]
        try:
            while waited <= deadline:
                resp = await self._client.get(url)
                if resp.status_code != 200:
                    raise DittobenchError(
                        f"poll rejected ({resp.status_code}): {resp.text[:200]}"
                    )
                data = resp.json()
                if not isinstance(data, dict):
                    raise DittobenchError("poll response was not a JSON object")
                snapshot = safe_progress_snapshot(data)
                if snapshot is not None:
                    snapshot = replace(snapshot, run_token=run_token)
                if snapshot is not None and progress_callback is not None:
                    try:
                        await progress_callback(snapshot)
                    except Exception:  # noqa: BLE001 - telemetry never gates scoring
                        logger.warning(
                            "dittobench progress callback failed; scoring continues"
                        )
                status = data.get("status")
                if status == _DONE:
                    rep = data.get("report")
                    details = rep.get("details") if isinstance(rep, dict) else None
                    reported_version = (
                        details.get("bench_version")
                        if isinstance(details, dict)
                        else None
                    )
                    job_version = data.get("bench_version")
                    # Fail CLOSED on any version. Enumerating 2 and 3 with no
                    # fallthrough meant a lease at an unknown version -- 4 after
                    # the next bump, or None -- was scored with no job/report
                    # verification at all. The check is the same for every
                    # version, so express it once rather than per-version.
                    if (
                        job_version != expected_bench_version
                        or reported_version != expected_bench_version
                    ):
                        raise DittobenchError(
                            "benchmark version mismatch: "
                            f"ticket={expected_bench_version!r} "
                            f"job={job_version!r} report={reported_version!r}"
                        )
                    # Offline reproducibility: fetch the run's transcript
                    # artifact and bind its digest into the report details, so
                    # the score signature covers it and the worker can publish
                    # the bytes. Never gates scoring: a missing or corrupt
                    # transcript logs and the score submits without one.
                    digest = await self._fetch_transcript(
                        run_id, data.get("transcript_sha256")
                    )
                    if digest is not None and isinstance(rep, dict):
                        details = rep.get("details")
                        if not isinstance(details, dict):
                            details = {}
                        details["transcript_sha256"] = digest
                        rep["details"] = details
                    self.last_details = (
                        rep["details"]
                        if isinstance(rep, dict)
                        and isinstance(rep.get("details"), dict)
                        else {}
                    )
                    parsed = self._parse_report(data)
                    # The score-signature domain is version-generic: signing.py
                    # appends ``:{bench_version}`` whenever the report carries
                    # one. Stamp the ACTUAL scored version so v4 (and any later
                    # bump) signs its own domain rather than v3's. v2 leaves the
                    # field unset, keeping those reports byte-compatible with
                    # old platforms/scorers.
                    return (
                        parsed.model_copy(
                            update={"bench_version": expected_bench_version}
                        )
                        if expected_bench_version >= _SCREENED_IMAGE_BENCH_VERSION
                        else parsed
                    )
                if status == _FAILED:
                    error = str(data.get("error", "unknown"))
                    infrastructure_code = _sandbox_infrastructure_failure_code(data)
                    if infrastructure_code == "sandbox_oom":
                        raise SandboxOomError(
                            f"run {run_id} exhausted the sandbox memory allowance"
                        )
                    if infrastructure_code is not None:
                        raise ValidatorInfrastructureError(
                            f"run {run_id} reported validator infrastructure "
                            f"failure: {infrastructure_code}"
                        )
                    if _is_embedding_infrastructure_failure(error):
                        raise ValidatorInfrastructureError(
                            f"run {run_id} lost validator embedding infrastructure: "
                            f"{error}"
                        )
                    raise DittobenchError(f"run {run_id} failed: {error}")
                await asyncio.sleep(self._config.dittobench_poll_seconds)
                waited += self._config.dittobench_poll_seconds
        except httpx.HTTPError as e:
            raise DittobenchError(f"poll failed: {e}") from e
        except asyncio.CancelledError:
            await self._cancel(run_id)
            raise
        await self._cancel(run_id)
        raise DittobenchError(
            f"run {run_id} did not finish within "
            f"{self._config.dittobench_timeout_seconds}s"
        )

    async def _fetch_transcript(self, run_id: str, declared: object) -> str | None:
        """Fetch + digest-verify the run's transcript; stash it on the client.

        Returns the verified digest, or ``None`` (with ``last_transcript``
        cleared) when the run declared no transcript, the fetch failed, or the
        bytes do not hash to the declared digest. Never raises: the score does
        not depend on the artifact.
        """
        self.last_transcript = None
        self._transcripts.pop(run_id, None)
        if not isinstance(declared, str) or not declared:
            return None
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}/transcript"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as e:
            logger.warning("run %s transcript fetch failed: %s", run_id, e)
            return None
        if resp.status_code != 200:
            logger.warning(
                "run %s transcript fetch rejected (%d)", run_id, resp.status_code
            )
            return None
        body = resp.content
        digest = hashlib.sha256(body).hexdigest()
        if digest != declared:
            logger.warning(
                "run %s transcript digest mismatch (declared %s, got %s); "
                "dropping the artifact",
                run_id,
                declared,
                digest,
            )
            return None
        self.last_transcript = body
        self._transcripts[run_id] = body
        while len(self._transcripts) > 16:
            self._transcripts.pop(next(iter(self._transcripts)))
        return digest

    async def _cancel(self, run_id: str) -> None:
        """Best-effort cancellation so a timed-out run cannot keep the sandbox.

        Older scorer revisions do not expose DELETE yet; a failed cancellation
        is logged but never hides the original validator timeout.
        """
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        try:
            resp = await self._client.delete(url)
            if resp.status_code not in (200, 202, 404, 405):
                logger.warning(
                    "dittobench run %s cancellation rejected (%d): %s",
                    run_id,
                    resp.status_code,
                    resp.text[:200],
                )
        except httpx.HTTPError as e:
            logger.warning("dittobench run %s cancellation failed: %s", run_id, e)

    @staticmethod
    def _parse_report(job: dict) -> ScoreReport:
        report = job.get("report")
        if not isinstance(report, dict):
            raise DittobenchError("done run missing report object")
        # The dittobench ScoreReport omits the seed (it lives on the job); the
        # platform ScoreReport carries it, so inject it before validating.
        report.setdefault("seed", job.get("seed", 0))
        try:
            return ScoreReport.model_validate(report)
        except Exception as e:  # noqa: BLE001 - surface any shape drift as our error
            raise DittobenchError(f"could not parse ScoreReport: {e}") from e
