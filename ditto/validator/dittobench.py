"""Async client for the hosted dittobench-api scoring engine.

Drives the v8 run-size pipeline over HTTP: ``POST /v2/score`` with the platform's
presigned ``tarball_url`` and pinned dataset, then polls
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
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4

import httpx

from ditto.api_models.benchmark_progress import (
    MAX_BENCHMARK_CHECKS,
    BenchmarkProgressStage,
)
from ditto.api_models.validator import (
    ArtifactResponse,
    BenchmarkRuntimeSettings,
    ScoreReport,
)
from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ScorerLivenessProbe,
    ScorerProbeOutcome,
    ScorerProbeReason,
    ValidatorStackIdentity,
)
from ditto.api_models.validator_confirmation import (
    ConfirmationBundleMode,
    V9ConfirmationJobResponse,
    V9ConfirmationScorerReadiness,
    V9ConfirmationScorerRequest,
    V9ConfirmationScorerResult,
)
from ditto.validator.config import (
    LEASE_REPORT_MARGIN_SECONDS,
    lease_budget_seconds,
    run_budget_seconds,
)
from ditto.validator.errors import (
    DittobenchError,
    LeaseDeadlineError,
    SandboxOomError,
    ValidatorInfrastructureError,
)
from ditto_screening_protocol.bench_v9 import supports_confirmation

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

# Terminal job states reported by dittobench-api's store.
_DONE = "done"
_FAILED = "failed"

# Hard bound on the best-effort run cancellation that follows an abort. Small
# by design: it is spent out of the lease's reporting margin.
_CANCEL_TIMEOUT_SECONDS = 15.0
_UNCHANGED_PROGRESS_TIMEOUT_SECONDS = 15 * 60.0
# A full scorer can clear as sibling runs finalize. Keep the leased job in hand
# long enough to ride out that local hand-off instead of expiring it and
# restarting the same 351 cases, but never spend an unbounded share of a lease
# waiting outside the scorer.
_SCORER_ADMISSION_RETRY_SECONDS = 5 * 60.0
_SCORER_ADMISSION_MAX_BACKOFF_SECONDS = 15.0

_CONFIRMATION_FAILURE_CLASS_HEADER = "X-Ditto-Confirmation-Failure-Class"
_CONFIRMATION_FAILURE_STATUS_HEADER = "X-Ditto-Confirmation-Failure-Status"
_CONFIRMATION_ZERO_STATUS_FAILURES = frozenset(
    {
        "longmem_seed_request",
        "longmem_seed_response_too_large",
        "longmem_seed_incomplete_ack",
        "longmem_run_request",
        "longmem_run_response_too_large",
        "longmem_run_missing_final_text",
    }
)
_CONFIRMATION_HTTP_STATUS_FAILURES = frozenset(
    {"longmem_seed_http_status", "longmem_run_http_status"}
)
_CONFIRMATION_MALFORMED_STATUS_FAILURES = frozenset(
    {"longmem_seed_malformed_json", "longmem_run_malformed_json"}
)
_CONFIRMATION_RESPONSE_READ_FAILURES = frozenset(
    {"longmem_seed_response_read", "longmem_run_response_read"}
)

_PROGRESS_STAGE_BY_STATUS: dict[str, BenchmarkProgressStage] = {
    "queued": "preparing",
    "building": "building_harness",
    "generating": "generating_dataset",
    "seeding": "starting_harness",
    "running": "running_benchmark",
    "waiting_for_relay": "waiting_for_relay",
    "scoring": "finalizing",
    "done": "finalizing",
    "failed": "failed_retrying",
}
_STABLE_COUNT_STATUSES = {"running", "waiting_for_relay", "scoring", "done"}
_PROGRESS_STAGE_ORDER: dict[BenchmarkProgressStage, int] = {
    "preparing": 0,
    "building_harness": 1,
    "generating_dataset": 2,
    "starting_harness": 3,
    "running_benchmark": 4,
    "waiting_for_relay": 4,
    "finalizing": 5,
    "submitting_result": 6,
    "failed_retrying": 7,
}


def _confirmation_failure_diagnostic(response: httpx.Response) -> str:
    """Return one bounded scorer diagnostic, or nothing for any drift.

    The confirmation response body deliberately remains stage-only. These
    purpose-specific headers cross only the protected local scorer boundary,
    and are accepted solely when their class/status pair matches a shape the
    Go scorer can emit from its reviewed allowlist.
    """
    failure_class = response.headers.get(_CONFIRMATION_FAILURE_CLASS_HEADER)
    raw_status = response.headers.get(_CONFIRMATION_FAILURE_STATUS_HEADER)
    if failure_class is None or raw_status is None:
        return ""
    if (
        not raw_status
        or len(raw_status) > 3
        or not raw_status.isascii()
        or not raw_status.isdecimal()
    ):
        return ""
    status = int(raw_status)
    if raw_status != str(status) or status > 599:
        return ""
    if failure_class in _CONFIRMATION_ZERO_STATUS_FAILURES:
        valid = status == 0
    elif failure_class in _CONFIRMATION_HTTP_STATUS_FAILURES:
        valid = 100 <= status <= 599
    elif failure_class in _CONFIRMATION_MALFORMED_STATUS_FAILURES:
        valid = 200 <= status <= 299
    elif failure_class in _CONFIRMATION_RESPONSE_READ_FAILURES:
        valid = status == 0 or 100 <= status <= 599
    else:
        valid = False
    if not valid:
        return ""
    return f" [failure_class={failure_class} failure_status={status}]"


_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SOFTWARE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,63}$")

# Executable scorer contracts. This is deliberately separate from the
# platform-selected active benchmark: validators can advertise and execute v9
# before the rollout control plane starts issuing v9 tickets.
#
# This list is intersected with what the scorer advertises
# (``scorer_benchmark_capability``), so a version the scorer supports is dropped
# from the validator's signed heartbeat unless it also appears here. It must
# track the dittobench-api ``supportedBenchVersions`` set: v11 shipped there and
# in the Platform, but was omitted here, so validators advertised only [8, 9, 10]
# and the Platform counted zero v11-capable validators while v8/9/10 kept working.
# v12 (anti-KV-substrate contract + causal model-dependence score gate) is now
# executable in the scorer, so it is advertised here too; on-chain activation
# remains a separate Platform rollout step.
SUPPORTED_BENCH_VERSIONS: tuple[int, ...] = (8, 9, 10, 11, 12)


# Scorer identity faults. Both stop benchmark advertisement and both
# surface as an ``identity_mismatch`` scorer status in the heartbeat; the names
# exist so an operator can tell the two causes apart in logs and telemetry.
#
# ``scorer_revision_mismatch``: the scorer reports a different revision than the
# committed pin — the ordinary, already-detectable case.
# ``scorer_image_stale``: the scorer reports the pinned revision but is not
# running it. Either it says so itself (binary and environment disagree) or it
# cannot serve the benchmark version its pinned revision is known to support.
_SCORER_REVISION_MISMATCH = "scorer_revision_mismatch"
_SCORER_IMAGE_STALE = "scorer_image_stale"

# ``source_revision_origin`` value meaning the revision was compiled into the
# running binary. Any other value — including its absence on an older scorer —
# means the revision is only asserted by the process environment.
_ORIGIN_BINARY = "binary"

_SANDBOX_INFRASTRUCTURE_CODES = {
    "sandbox_oom",
    "sandbox_tmpfs_exhausted",
    # The scorer could not establish its validator-owned sandbox network path:
    # either the egress network was missing at container start or the observed
    # tool listener could not self-verify. This is validator infrastructure,
    # not the agent's fault, so it must end the sweep and back off rather than
    # blame and re-lease the agent in a tight resubmit loop.
    "sandbox_network_unavailable",
    # The ticket inference route failed or its upstream provider degraded
    # mid-run. Also validator-side infrastructure, so
    # back off instead of failing the agent and re-leasing it in a loop.
    "model_relay_unavailable",
    # The platform-owned hosted embedding route failed or could not prove
    # complete delivery. The scorer fails closed and the validator retries the
    # whole ticket later; the agent must never receive a score for this attempt.
    "embedding_provider_unavailable",
    # The scorer could not ACQUIRE the screened image the platform itself
    # produced -- transport error, object-store 5xx/429, a local scratch-disk
    # fault, or a stream truncated mid-download. Charging that to the miner
    # spends one of their finite attempts on an outage they did not cause and
    # could not have influenced: every failure carrying this code happens
    # strictly before ``docker run``, so the harness has not executed an
    # instruction. Verification failures (sha256/size/image-id mismatch,
    # malformed archive, 4xx other than 429, docker image load) are deliberately
    # NOT given this code by the scorer -- they are deterministic in the bytes
    # the platform stored, and a no-fault verdict would re-lease a permanently
    # broken image without bound.
    "screened_image_unavailable",
    # The scorer could not enter its validator-owned seed store for the full
    # bounded 600-second lock wait. The harness never ran, so consuming the
    # miner's attempt would charge them for shared scorer infrastructure.
    "seed_store_lock_timeout",
}


# The other half of the same dittobench-api change, recorded here because this
# set is where a future edit would be tempted to "finish the job".
#
# Note what is deliberately NOT here: a code for inference-lane saturation.
# dittobench-api distinguishes a saturated platform lane in the failure's
# DIAGNOSTICS and keeps ``model_relay_unavailable`` as the code, precisely so it
# needs no entry in this set. Adding a code here only helps validators that have
# already rolled; ``_sandbox_infrastructure_failure_code`` returns None for
# anything outside this set, and None becomes fail_job("scoring_error"), so a
# NEW no-fault code charges the miner on every validator that predates it. The
# fleet lags scorer releases by design. Agent codes have no such hazard -- an
# old validator's terminal default is already the intended outcome for them --
# which is why only this direction needs the care.
#
# The scorer emits two terminal, agent-attributable inference codes:
#
# - ``inference_allowance_exhausted`` when the harness spent the request-count
#   or token allowance its own ticket granted, or sent one request too large to
#   reserve (platform decline codes 4102/4104/4109).
# - ``model_inference_required`` when the broker stayed healthy but observed no
#   authoritative chat request during the scored interval AND the scorer could
#   not prove the harness was able to reach the broker at all.
#
# The lease and broker were healthy in both cases. A fresh grant therefore
# cannot repair the same harness behaviour.
#
# Read the second one carefully, because it no longer means "the agent ran no
# inference". On every gated version (v9, v10, v11) a zero-inference run whose
# route disposition the scorer DID prove -- one routed discarded challenge, or
# both supported selectors challenged and missed -- is not a failure at all. It
# comes back through the ordinary ``/score`` path as an accepted, signed
# composite of 0.00 with the model-use gate reading ``zero_inference``, and this
# validator submits it like any other score. Nothing here special-cases it, and
# nothing should: a 0.00 is a score, and the ledger needs it to exist so the
# agent stops being ranked on its previous bench version's composite.
#
# ``model_inference_required`` is therefore now the strictly narrower residue:
# a zero-chat interval the scorer could not distinguish from an inference-adapter
# mismatch. That ambiguity is exactly why it must not be scored 0.00 -- and also
# why it must not become retryable, since re-leasing re-runs the same image
# against the same selectors.
#
# It MUST NOT be added to ``_SANDBOX_INFRASTRUCTURE_CODES``. Every code in that
# set is no-fault: it mints a retry grant, RAISES the attempt cap, and
# re-leases. An agent that reliably exhausts its own allowance would therefore
# re-lease itself forever -- exactly the loop that let the mnemox family reach
# far past its attempt budget with zero scores while holding validator slots.
#
# Belt and braces: the scorer sends this as ``sandbox_failure`` /
# ``retryable: false``, and ``_sandbox_infrastructure_failure_code`` requires
# ``validator_infrastructure`` AND ``retryable is True`` before it even looks at
# the code. So the agent codes are excluded three independent ways. The test
# ``test_agent_attributable_inference_failures_stay_the_agents`` pins all three.
_AGENT_ATTRIBUTABLE_INFERENCE_CODES = frozenset(
    {"inference_allowance_exhausted", "model_inference_required"}
)

assert not (_AGENT_ATTRIBUTABLE_INFERENCE_CODES & _SANDBOX_INFRASTRUCTURE_CODES)


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


_ROUTE_PROOF_UNAVAILABLE = "route_proof_unavailable"


def _platform_route_proof_gap(payload: dict[str, object]) -> bool:
    """Whether the scorer reported an unfinished platform route challenge.

    Deliberately its own narrow shape rather than a new entry in
    ``_SANDBOX_INFRASTRUCTURE_CODES``: every code in that set is no-fault and
    mints a retry grant that RAISES the attempt cap, which is the unbounded
    re-lease loop. This one must retry inside the ORDINARY attempt budget --
    recoverable, because the next run challenges the route again, but bounded,
    because a systematically unprobeable submission must still run out of
    attempts instead of holding validator slots forever.
    """
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return False
    return (
        failure.get("kind") == "validator_infrastructure"
        and failure.get("retryable") is True
        and failure.get("code") == _ROUTE_PROOF_UNAVAILABLE
    )


def _agent_attributable_failure_code(payload: dict[str, object]) -> str | None:
    """Accept only the scorer's terminal, source-free agent classifier.

    This is deliberately the mirror image of
    :func:`_sandbox_infrastructure_failure_code`: the kind must be the terminal
    sandbox class and ``retryable`` must be exactly false.  Keeping the code
    through :class:`DittobenchError` lets ``failure_detail`` publish the safe
    reason instead of collapsing it into an opaque ``scoring_error`` string.
    """
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return None
    if (
        failure.get("kind") != "sandbox_failure"
        or failure.get("retryable") is not False
    ):
        return None
    code = failure.get("code")
    return (
        code
        if isinstance(code, str) and code in _AGENT_ATTRIBUTABLE_INFERENCE_CODES
        else None
    )


_RELAY_CAUSES = frozenset(
    {
        "inference_lane_saturated",
        "provider_recovery_exhausted",
    }
)


def _sandbox_infrastructure_failure_detail(
    payload: dict[str, object], code: str
) -> str:
    """Keep the deployed code stable while preserving an allowlisted cause."""
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return code
    diagnostics = failure.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return code
    cause = diagnostics.get("relay_cause")
    if not isinstance(cause, str) or cause not in _RELAY_CAUSES:
        return code
    return f"{code}:{cause}"


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
        self.full_run_capacity = (
            int(getattr(config, "benchmark_capacity", 1))
            if getattr(config, "dittobench_mock", False)
            else 1
        )
        # Why the scorer's running identity could not be verified, or ``None``
        # while it verifies. Read by telemetry and by the log de-duplication
        # below; the heartbeat carries the same finding as an
        # ``identity_mismatch`` scorer status.
        self.scorer_identity_fault: str | None = None
        self._scorer_identity_fault_logged: str | None = None
        # Liveness evidence for the heartbeat: when the scorer last answered
        # with a capability document this validator could read whole, and how
        # many probes have failed to get one since. Process-local by design —
        # after a restart the validator knows nothing about the past and says
        # so, rather than carrying a claim it cannot support.
        self._scorer_last_served_at: int | None = None
        self._scorer_probe_failures = 0

    def take_transcript(self, run_id: str) -> bytes | None:
        """Consume the verified transcript belonging to exactly ``run_id``."""
        return self._transcripts.pop(run_id, None)

    def _control_headers(self) -> dict[str, str]:
        """Authorize this validator on the scorer's inference control plane.

        The scorer admits ``/v1/inference/session*`` from a loopback peer or a
        matching bearer. It joins sandbox-docker's network namespace while this
        worker stays on the Compose bridge, so the call always arrives from a
        private-bridge address and the bearer is the only thing that can
        authorize it. Sent on the control plane alone — the shared
        ``httpx.AsyncClient`` also talks to the platform, so this must never
        become a client-wide default header.
        """
        token = str(getattr(self._config, "dittobench_control_token", "") or "")
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def v9_confirmation_readiness(
        self,
    ) -> V9ConfirmationScorerReadiness | None:
        """Return the exact internal profile, without advertising benchmark v9."""
        try:
            response = await self._client.get(
                f"{self._config.dittobench_api_url}/v1/confirmation/readiness",
                headers=self._control_headers(),
            )
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"v9 confirmation readiness failed: {error}"
            ) from error
        if response.status_code == 503:
            return None
        if response.status_code != 200:
            raise ValidatorInfrastructureError(
                f"v9 confirmation readiness rejected ({response.status_code})"
            )
        try:
            return V9ConfirmationScorerReadiness.model_validate(response.json())
        except (TypeError, ValueError) as error:
            raise ValidatorInfrastructureError(
                "v9 confirmation readiness response was invalid"
            ) from error

    async def execute_v9_confirmation(
        self,
        *,
        job: V9ConfirmationJobResponse,
        artifact: ArtifactResponse,
        inference_session_id: str,
    ) -> V9ConfirmationScorerResult:
        """Execute one costly bundle through the protected local control plane."""
        if (
            job.purpose != "v9_confirmation_bundle"
            or not supports_confirmation(job.bench_version)
            or artifact.agent_id != job.agent_id
            or artifact.sha256 != job.artifact_sha256
            or not supports_confirmation(artifact.bench_version)
            or artifact.bench_version != job.bench_version
        ):
            raise DittobenchError("v9 confirmation job/artifact identity mismatch")
        screened_identity = (
            artifact.screened_image_url,
            artifact.screened_image_sha256,
            artifact.screened_image_size_bytes,
            artifact.screened_image_id,
            artifact.screened_image_ref,
        )
        if any(value is None for value in screened_identity):
            raise DittobenchError(
                "v9 confirmation requires a complete screened image identity"
            )
        assert artifact.screened_image_url is not None
        assert artifact.screened_image_sha256 is not None
        assert artifact.screened_image_size_bytes is not None
        assert artifact.screened_image_id is not None
        assert artifact.screened_image_ref is not None
        if job.mode is ConfirmationBundleMode.OFF:
            raise DittobenchError("v9 confirmation execution cannot run in off mode")
        mode = cast(Literal["shadow", "enforce"], job.mode.value)
        request = V9ConfirmationScorerRequest(
            purpose=job.purpose,
            bundle_id=job.bundle_id,
            ticket_id=job.ticket_id,
            agent_id=job.agent_id,
            slot_id=job.slot_id,
            inference_session_id=inference_session_id,
            artifact_url=artifact.download_url,
            artifact_sha256=job.artifact_sha256,
            screened_image_url=artifact.screened_image_url,
            screened_image_sha256=artifact.screened_image_sha256,
            screened_image_size_bytes=artifact.screened_image_size_bytes,
            screened_image_id=artifact.screened_image_id,
            screened_image_ref=artifact.screened_image_ref,
            bench_version=job.bench_version,
            deadline=job.deadline,
            profile_revision=job.execution_profile.revision,
            profile_checksum=job.execution_profile.checksum,
            settings_revision=job.settings_revision,
            settings_checksum=job.settings_checksum,
            retest_generation=job.retest_generation,
            mode=mode,
            per_bundle_request_cap=job.per_bundle_request_cap,
            per_bundle_token_cap=job.per_bundle_token_cap,
            execution_profile=job.execution_profile,
        )
        budget = lease_budget_seconds(job.deadline)
        if budget <= 0:
            raise LeaseDeadlineError(
                "v9 confirmation ticket cannot fund execution while preserving "
                "its reporting margin"
            )
        try:
            response = await self._client.post(
                f"{self._config.dittobench_api_url}/v1/confirmation/execute",
                headers=self._control_headers(),
                json=request.model_dump(mode="json"),
                # This protected call owns the complete bounded confirmation
                # execution.  The client's ordinary short request timeout is
                # suitable for control calls but would kill a valid LongMem
                # run, so bind this one call to the ticket-derived budget.
                timeout=budget,
            )
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"v9 confirmation execution failed: {error}"
            ) from error
        if response.status_code != 200:
            diagnostic = _confirmation_failure_diagnostic(response)
            raise DittobenchError(
                "v9 confirmation execution rejected "
                f"({response.status_code}): {response.text[:200]}{diagnostic}"
            )
        try:
            return V9ConfirmationScorerResult.model_validate(response.json())
        except (TypeError, ValueError) as error:
            raise DittobenchError("v9 confirmation result was invalid") from error

    async def prepare_inference_session(self) -> InferenceBrokerSession:
        """Create a trusted memory-only broker key before claiming provider access."""
        try:
            response = await self._client.post(
                f"{self._config.dittobench_api_url}/v1/inference/session",
                headers=self._control_headers(),
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
        request_budget: int | None = None,
        token_budget: int | None = None,
        embedding_request_budget: int | None = None,
        embedding_token_budget: int | None = None,
        max_output_tokens: int | None = None,
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
            for name, value in (
                ("request_budget", request_budget),
                ("token_budget", token_budget),
                ("embedding_request_budget", embedding_request_budget),
                ("embedding_token_budget", embedding_token_budget),
                ("max_output_tokens", max_output_tokens),
            ):
                if value is not None:
                    activation[name] = value
            response = await self._client.post(
                f"{self._config.dittobench_api_url}/v1/inference/session/"
                f"{session.session_id}/activate",
                json=activation,
                headers=self._control_headers(),
            )
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"inference broker activation failed: {error}"
            ) from error
        if response.status_code != 200:
            raise ValidatorInfrastructureError("inference broker activation rejected")

    async def activate_confirmation_inference_session(
        self,
        session: InferenceBrokerSession,
        *,
        job: V9ConfirmationJobResponse,
    ) -> None:
        """Atomically install the three purpose-bound confirmation grants."""
        payload = {
            "activation_secret": session.activation_secret,
            "agent_id": str(job.agent_id),
            "slot_id": job.slot_id,
            "ticket_deadline": job.deadline.isoformat(),
            "grants": [grant.model_dump(mode="json") for grant in job.inference_grants],
        }
        try:
            response = await self._client.post(
                f"{self._config.dittobench_api_url}/v1/inference/session/"
                f"{session.session_id}/activate-confirmation",
                json=payload,
                headers=self._control_headers(),
            )
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"confirmation inference broker activation failed: {error}"
            ) from error
        if response.status_code != 200:
            raise ValidatorInfrastructureError(
                "confirmation inference broker activation rejected"
            )

    async def cancel_inference_session(self, session_id: str) -> None:
        """Best-effort deletion for pre-run failures and completed sessions."""
        with contextlib.suppress(httpx.HTTPError):
            await self._client.delete(
                f"{self._config.dittobench_api_url}/v1/inference/session/{session_id}",
                headers=self._control_headers(),
            )

    def _record_scorer_probe(
        self,
        outcome: ScorerProbeOutcome,
        *,
        observed_at: int,
        http_status: int | None = None,
        reason: ScorerProbeReason | None = None,
    ) -> ScorerLivenessProbe:
        """Fold one probe result into the liveness this heartbeat reports.

        ``last_served_at`` advances only on a fully readable document. A reply
        the validator had to narrow is evidence the scorer is answering, not
        evidence it is serving what it claims — the difference that made a whole
        fleet look healthy while it had silently lost the active benchmark.

        ``not_probed`` is neither: mock mode observes nothing, so it neither
        counts a failure nor claims service.
        """
        if outcome == "served":
            self._scorer_probe_failures = 0
            self._scorer_last_served_at = observed_at
        elif outcome != "not_probed":
            self._scorer_probe_failures += 1
        return ScorerLivenessProbe(
            outcome=outcome,
            observed_at=observed_at,
            http_status=http_status,
            reason=reason,
            last_served_at=self._scorer_last_served_at,
            consecutive_failures=self._scorer_probe_failures,
        )

    async def scorer_benchmark_capability(
        self, stack: ValidatorStackIdentity
    ) -> ScorerBenchmarkCapability:
        """Observe scorer support and remember what was advertised.

        The observation itself lives in
        :meth:`_observe_scorer_benchmark_capability`, which has many fail-closed
        exits. This wrapper is the single place that latches the result, so
        :meth:`preflight` can never read a stale advertisement from an earlier
        sweep no matter which exit was taken.
        """
        return await self._observe_scorer_benchmark_capability(stack)

    async def _observe_scorer_benchmark_capability(
        self, stack: ValidatorStackIdentity
    ) -> ScorerBenchmarkCapability:
        """Observe scorer support and bind it to signed stack identity.

        Missing routes, malformed replies, timeouts, source mismatches, and
        retired-only capability claims advertise no work. Forward-version claims
        may accompany v8/v9, but this validator publishes only the intersection
        of contracts it can execute.

        The revision the scorer reports is only trustworthy when it is a
        property of the running binary. ``source_revision_origin`` says which it
        is, and ``source_revision_mismatch`` reports that the binary and the
        environment disagreed — the signature of a container recreated against a
        cached image. When the claim is merely environment-asserted, the
        capability set the scorer actually serves is the remaining evidence: a
        scorer that cannot advertise the version its pinned revision is known to
        support is not running that revision. Either way the result is a
        *reported* mismatch, never a crash: the validator keeps heartbeating,
        advertises no work and shows up degraded instead of scoring blind.
        """
        observed_at = int(time.time())
        if self._config.dittobench_mock:
            # Local plumbing mode performs no observation. Reporting one would
            # be an invention, so it reports that none was made.
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe("not_probed", observed_at=observed_at),
            )
        self.full_run_capacity = 1
        try:
            response = await self._client.get(
                f"{self._config.dittobench_api_url}/v1/capabilities",
                timeout=getattr(
                    self._config, "dittobench_capabilities_timeout_seconds", 3.0
                ),
            )
        except httpx.HTTPError as error:
            # A deadline and a refused connection are both "no answer", but they
            # are different faults with different fixes: one is a wedged scorer,
            # the other is a scorer that is not listening at all.
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "connect_error",
                    observed_at=observed_at,
                ),
            )
        if response.status_code == 404:
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "http_error", observed_at=observed_at, http_status=404
                ),
            )
        if response.status_code != 200:
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "http_error",
                    observed_at=observed_at,
                    http_status=response.status_code,
                ),
            )
        try:
            payload = response.json()
        except ValueError:
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="invalid_json",
                ),
            )
        if not isinstance(payload, dict):
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="malformed_capabilities",
                ),
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
            or any(version <= 0 for version in versions)
            or type(full_run_capacity) is not int
            or not 1 <= full_run_capacity <= 8
        ):
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="malformed_capabilities",
                ),
            )
        advertised_versions = tuple(sorted(set(versions)))
        # Capability negotiation is an intersection, not an assertion that both
        # processes advertise identical histories. During a rolling update an
        # older scorer may still describe a retired contract alongside v8. The
        # validator must ignore that extra metadata and expose only contracts it
        # can actually execute; retired-only scorers still fail the
        # empty-intersection check below.
        observed_versions = tuple(
            version
            for version in advertised_versions
            if version in SUPPORTED_BENCH_VERSIONS
        )
        if not observed_versions:
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="unsupported_bench_version",
                ),
            )
        expected_revision = stack.components.dittobench_api.source_revision
        fault = self._scorer_identity_fault(
            payload,
            source_revision=source_revision,
            expected_revision=expected_revision,
        )
        if fault is not None:
            self._log_scorer_identity_fault(
                fault,
                source_revision=source_revision,
                expected_revision=expected_revision,
                software_version=software_version,
                observed_versions=observed_versions,
                origin=payload.get("source_revision_origin"),
            )
            return ScorerBenchmarkCapability(
                status="identity_mismatch",
                supported_bench_versions=(),
                observed_at=observed_at,
                software_version=software_version,
                source_revision=source_revision,
                probe=self._record_scorer_probe(
                    "served_degraded",
                    observed_at=observed_at,
                    http_status=200,
                    reason="identity_mismatch",
                ),
            )
        self._log_scorer_identity_fault(
            None,
            source_revision=source_revision,
            expected_revision=expected_revision,
            software_version=software_version,
            observed_versions=observed_versions,
            origin=payload.get("source_revision_origin"),
        )
        self.full_run_capacity = full_run_capacity
        try:
            return ScorerBenchmarkCapability(
                status="fresh_verified",
                supported_bench_versions=observed_versions,
                observed_at=observed_at,
                software_version=software_version,
                source_revision=source_revision,
                probe=self._record_scorer_probe(
                    "served",
                    observed_at=observed_at,
                    http_status=200,
                ),
            )
        except ValueError:
            # Defensive: every constraint above is already satisfied here. If a
            # future one is not, the reply was not usable after all, so report
            # that rather than a capability nothing verified.
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="malformed_capabilities",
                ),
            )

    def _scorer_identity_fault(
        self,
        payload: dict[str, object],
        *,
        source_revision: str,
        expected_revision: str | None,
    ) -> str | None:
        """Name why the running scorer is not the pinned one, or ``None``.

        Strongest evidence first. A revision that differs from the pin is a
        plain mismatch. A scorer reporting that its binary and its environment
        named different commits is a stale image by its own admission. A
        revision compiled into the running binary and equal to the pin is proof
        and ends the check. Anything else is an assertion by the environment,
        which is exactly what lied during the v0.29.3 incident — so when the pin
        is one that stamps its identity at build time, an unstamped answer means
        the running image is not that pin.
        """
        if source_revision != expected_revision:
            return _SCORER_REVISION_MISMATCH
        # Always emitted by a scorer new enough to have an opinion, so ``False``
        # is never ambiguous with an older scorer that omits it.
        if payload.get("source_revision_mismatch") is True:
            return _SCORER_IMAGE_STALE
        if payload.get("source_revision_origin") == _ORIGIN_BINARY:
            return None
        # Absent (older scorer) or "env" (nothing was linked in, including the
        # case where a mistyped -X silently produced an unstamped binary). Both
        # mean the same thing: this is not provably the pinned revision.
        return (
            _SCORER_IMAGE_STALE
            if getattr(self._config, "scorer_require_binary_provenance", False)
            else None
        )

    def _log_scorer_identity_fault(
        self,
        fault: str | None,
        *,
        source_revision: str,
        expected_revision: str | None,
        software_version: str,
        observed_versions: tuple[int, ...],
        origin: object,
    ) -> None:
        """Surface an identity fault once per transition, never once per sweep.

        The scorer capability probe runs on every heartbeat, so an unconditional
        warning would bury the change itself in repetition. Recovery is logged
        too: an operator watching for the fix needs to see it clear.
        """
        self.scorer_identity_fault = fault
        if fault == self._scorer_identity_fault_logged:
            return
        self._scorer_identity_fault_logged = fault
        if fault is None:
            logger.info(
                "scorer identity verified: revision=%s (provenance=%s) "
                "version=%s bench=%s",
                source_revision,
                origin if isinstance(origin, str) else "unstamped",
                software_version,
                list(observed_versions),
            )
            return
        logger.error(
            "%s: the running scorer is not the pinned dittobench-api revision "
            "(reported revision=%s with provenance=%s, pinned revision=%s, "
            "reported version=%s, advertised bench versions=%s). The validator "
            "is degraded and will advertise no benchmark work until the scorer "
            "image is rebuilt from the pin; restarting the validator will not "
            "help. See docs/VALIDATOR.md, 'Stale scorer image'.",
            fault,
            source_revision,
            origin if isinstance(origin, str) else "unstamped",
            expected_revision,
            software_version,
            list(observed_versions),
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
        ticket_deadline: datetime | None = None,
        benchmark_runtime: BenchmarkRuntimeSettings | None = None,
    ) -> ScoreReport:
        """Score a submission by its presigned tarball URL (mode B).

        Submits the scoring inputs, then polls until the run finishes.
        Raises :class:`DittobenchError` on a failed run or the overall timeout.

        ``ticket_deadline`` is the lease this run is being scored under. It
        bounds the poll independently of ``inference_ticket_deadline``, which
        travels to the inference broker. The scoring lease is independent, so
        the abort must not depend on whether a broker session was established.

        ``tarball_sha256`` (the digest the platform registered at upload) is
        forwarded so the scorer re-verifies the fetched bytes against it and
        pins the Docker build tag to the content hash.

        ``seed`` pins the dataset seed. ``dataset_sha256`` selects the CANONICAL
        validator path: this posts to dittobench-api **/v2/score** with
        the platform-pinned ``seed`` + ``dataset_sha256`` (+ ``run_size``), so the
        engine regenerates that exact dataset and FAILS the run on a hash mismatch
        (tamper-evidence — every k=3 validator provably scored the platform's
        dataset). The deprecated unversioned scoring and implicit practice
        routes are not used.
        """
        if bench_version is None or bench_version not in SUPPORTED_BENCH_VERSIONS:
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
            ticket_deadline=ticket_deadline,
            benchmark_runtime=benchmark_runtime,
        )
        return await self._poll(
            run_id,
            progress_callback=progress_callback,
            expected_bench_version=bench_version,
            ticket_deadline=ticket_deadline,
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
        ticket_deadline: datetime | None = None,
        benchmark_runtime: BenchmarkRuntimeSettings | None = None,
    ) -> str:
        if bench_version is None or bench_version not in SUPPORTED_BENCH_VERSIONS:
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
        else:
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
            else:
                raise DittobenchError(
                    f"benchmark v{bench_version} requires ticket inference identity"
                )
        elif any(value is not None for value in inference_identity):
            raise DittobenchError(
                "ticket inference identity requires an inference session"
            )
        if not dataset_sha256:
            raise DittobenchError(
                f"benchmark v{bench_version} requires a pinned dataset"
            )
        body["dataset_sha256"] = dataset_sha256
        body["bench_version"] = bench_version
        if benchmark_runtime is not None:
            body["benchmark_runtime"] = benchmark_runtime.model_dump(mode="json")
        endpoint = "/v2/score"
        url = f"{self._config.dittobench_api_url}{endpoint}"
        admission_budget = (
            min(
                _SCORER_ADMISSION_RETRY_SECONDS,
                max(0.0, lease_budget_seconds(ticket_deadline)),
            )
            if ticket_deadline is not None
            else 0.0
        )
        admission_until = time.monotonic() + admission_budget
        backoff = 1.0
        while True:
            try:
                resp = await self._client.post(url, json=body)
            except httpx.HTTPError as e:
                raise DittobenchError(f"submit failed: {e}") from e
            if resp.status_code not in (429, 503):
                break
            remaining = admission_until - time.monotonic()
            if remaining <= 0:
                raise ValidatorInfrastructureError(
                    f"scorer admission unavailable ({resp.status_code})"
                )
            delay = min(
                backoff,
                _SCORER_ADMISSION_MAX_BACKOFF_SECONDS,
                remaining,
            )
            logger.info(
                "scorer admission returned %d; retrying in %.1fs with %.1fs "
                "left in the bounded admission window",
                resp.status_code,
                delay,
                remaining,
            )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2.0, _SCORER_ADMISSION_MAX_BACKOFF_SECONDS)
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
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        if (
            expected_bench_version is None
            or expected_bench_version not in SUPPORTED_BENCH_VERSIONS
        ):
            raise DittobenchError(
                f"unsupported benchmark version {expected_bench_version!r}"
            )
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        # Wall clock, and lease-derived. The previous budget was accumulated
        # ``dittobench_poll_seconds`` units, which charged nothing for the poll
        # request itself (bounded only by ``http_timeout_seconds``), the
        # progress callback, or the heartbeat it publishes -- so the nominal cap
        # had no upper bound in real time at all. And it started counting here,
        # leaving everything before it (artifact fetch, inference grant
        # exchange, submit) chargeable to the lease but not to the cap.
        started = time.monotonic()
        last_progress_at = started
        progress_stage = _PROGRESS_STAGE_ORDER["preparing"]
        progress_completed: int | None = None
        budget = run_budget_seconds(
            self._config.dittobench_timeout_seconds, ticket_deadline
        )
        lease_bound = (
            ticket_deadline is not None
            and budget < self._config.dittobench_timeout_seconds
        )
        # Opaque, stable-per-run token every progress heartbeat for this run
        # carries, so the platform can distinguish a fresh re-attempt of the same
        # lease from continued progress on the previous run and rebaseline the
        # monotonicity guard accordingly.
        run_token = hashlib.sha256(run_id.encode()).hexdigest()[:16]
        try:
            while time.monotonic() - started <= budget:
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
                    # the bytes. V8 keeps its legacy best-effort behavior. V9
                    # makes the transcript and typed base root part of the
                    # signed contract, so either must fail closed as validator
                    # infrastructure rather than escaping later from signing.
                    digest = await self._fetch_transcript(
                        run_id, data.get("transcript_sha256")
                    )
                    if expected_bench_version >= 9 and digest is None:
                        raise ValidatorInfrastructureError(
                            "benchmark v9+ transcript evidence unavailable"
                        )
                    if digest is not None and isinstance(rep, dict):
                        details = rep.get("details")
                        if not isinstance(details, dict):
                            details = {}
                        details["transcript_sha256"] = digest
                        rep["details"] = details
                    if expected_bench_version >= 9 and isinstance(rep, dict):
                        # Stamp before validation. ``model_copy(update=...)``
                        # deliberately skips validation, which previously let
                        # a V9 report bypass ScoreReport's typed base-evidence
                        # invariant and fail only when the worker signed it.
                        rep["bench_version"] = expected_bench_version
                        try:
                            parsed = self._parse_report(data)
                        except DittobenchError as parse_error:
                            self.last_transcript = None
                            self._transcripts.pop(run_id, None)
                            raise ValidatorInfrastructureError(
                                "benchmark v9 base evidence unavailable"
                            ) from parse_error
                    else:
                        parsed = self._parse_report(data)
                        # Preserve the V8 signing-domain stamp without making
                        # its legacy report shape subject to V9-only evidence.
                        parsed = parsed.model_copy(
                            update={"bench_version": expected_bench_version}
                        )
                    self.last_details = (
                        parsed.details if isinstance(parsed.details, dict) else {}
                    )
                    return parsed
                if status == _FAILED:
                    error = str(data.get("error", "unknown"))
                    agent_failure_code = _agent_attributable_failure_code(data)
                    infrastructure_code = _sandbox_infrastructure_failure_code(data)
                    if agent_failure_code is not None:
                        message = (
                            f"run {run_id} made no authoritative model call"
                            if agent_failure_code == "model_inference_required"
                            else f"run {run_id} exhausted its inference allowance"
                        )
                        raise DittobenchError(
                            message,
                            code=agent_failure_code,
                        )
                    # `code=` is what stops the scorer's own classifier from
                    # dying here. All five infrastructure codes collapse into
                    # one `infrastructure` hand-back, so without it the specific
                    # code survives only in a validator-host log line -- which is
                    # why ditto-subnet#279 could not name the ~60-minute
                    # `mnemo*` killer. It now reaches the ticket as
                    # `failure_detail`.
                    if infrastructure_code == "sandbox_oom":
                        raise SandboxOomError(
                            f"run {run_id} exhausted the sandbox memory allowance",
                            code=infrastructure_code,
                        )
                    if infrastructure_code is not None:
                        infrastructure_detail = _sandbox_infrastructure_failure_detail(
                            data, infrastructure_code
                        )
                        raise ValidatorInfrastructureError(
                            f"run {run_id} reported validator infrastructure "
                            f"failure: {infrastructure_code}",
                            code=infrastructure_detail,
                        )
                    if _platform_route_proof_gap(data):
                        # Not the agent's: the platform never finished proving
                        # the route disposition. Carrying the code keeps it out
                        # of an opaque ``scoring_error`` and puts the real
                        # reason on the ticket's ``failure_detail``.
                        raise DittobenchError(
                            f"run {run_id} could not prove its model route "
                            "disposition; the platform challenge was unfinished",
                            code=_ROUTE_PROOF_UNAVAILABLE,
                        )
                    raise DittobenchError(f"run {run_id} failed: {error}")
                observed_at = time.monotonic()
                if snapshot is not None:
                    observed_stage = _PROGRESS_STAGE_ORDER[snapshot.stage]
                    stage_advanced = observed_stage > progress_stage
                    count_advanced = (
                        observed_stage == progress_stage
                        and snapshot.completed is not None
                        and (
                            progress_completed is None
                            and snapshot.completed > 0
                            or progress_completed is not None
                            and snapshot.completed > progress_completed
                        )
                    )
                    if stage_advanced or count_advanced:
                        last_progress_at = observed_at
                        progress_stage = observed_stage
                        progress_completed = snapshot.completed
                if (
                    observed_at - last_progress_at
                    >= _UNCHANGED_PROGRESS_TIMEOUT_SECONDS
                ):
                    await self._cancel(run_id)
                    raise ValidatorInfrastructureError(
                        f"run {run_id} made no scoring progress for "
                        f"{_UNCHANGED_PROGRESS_TIMEOUT_SECONDS:.0f}s",
                        code="scorer_progress_stalled",
                    )
                # Never sleep past the budget: the abort must keep the whole
                # reporting margin, not the margin minus a poll interval.
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    break
                await asyncio.sleep(
                    min(self._config.dittobench_poll_seconds, remaining)
                )
        except httpx.HTTPError as e:
            raise DittobenchError(f"poll failed: {e}") from e
        except asyncio.CancelledError:
            await self._cancel(run_id)
            raise
        await self._cancel(run_id)
        elapsed = time.monotonic() - started
        if lease_bound and ticket_deadline is not None:
            # Name the binding constraint. An operator reading this must be able
            # to tell "your harness cap fired" from "the lease ran out first",
            # because only the second one means the cap was never the real
            # bound.
            raise LeaseDeadlineError(
                f"run {run_id} did not finish within the {budget:.0f}s its "
                f"lease could fund (ticket deadline "
                f"{ticket_deadline.isoformat()}, less a "
                f"{LEASE_REPORT_MARGIN_SECONDS:.0f}s reporting margin); "
                f"aborting after {elapsed:.0f}s so the ticket is resolved "
                "rather than left to expire"
            )
        raise DittobenchError(
            f"run {run_id} did not finish within "
            f"{self._config.dittobench_timeout_seconds}s"
        )

    async def _fetch_transcript(self, run_id: str, declared: object) -> str | None:
        """Fetch + digest-verify the run's transcript; stash it on the client.

        Returns the verified digest, or ``None`` (with ``last_transcript``
        cleared) when the run declared no transcript, the fetch failed, or the
        bytes do not hash to the declared digest. This helper never raises;
        the caller applies the active benchmark's evidence policy.
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

        Bounded well inside the reporting margin on purpose. This runs *after*
        the abort decision, so a scorer wedged badly enough to have caused the
        abort must not be able to spend the seconds the failure report needs.
        """
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        try:
            resp = await self._client.delete(url, timeout=_CANCEL_TIMEOUT_SECONDS)
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
