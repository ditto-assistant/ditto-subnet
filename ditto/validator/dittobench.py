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
from typing import TYPE_CHECKING, Literal
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
    ScorerLivenessProbe,
    ScorerProbeOutcome,
    ScorerProbeReason,
    V7InferenceCalibration,
    ValidatorStackIdentity,
)
from ditto.validator.config import LEASE_REPORT_MARGIN_SECONDS, run_budget_seconds
from ditto.validator.errors import (
    DittobenchError,
    LeaseDeadlineError,
    SandboxOomError,
    ValidatorInfrastructureError,
)

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

# Terminal job states reported by dittobench-api's store.
_DONE = "done"
_FAILED = "failed"

# Hard bound on the best-effort run cancellation that follows an abort. Small
# by design: it is spent out of the lease's reporting margin.
_CANCEL_TIMEOUT_SECONDS = 15.0

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

# Benchmark versions whose memory phase runs through the validator's OWN Ollama
# container -- the "local embedding lane". These are the only versions for which
# that container is a real dependency.
#
# From v7 the scorer embeds through the hosted platform lane instead. In
# dittobench-api the fork is per request -- ``benchVersion >= 7`` forwards to the
# platform's ``/api/v1/inference/embeddings``, below it forwards to this host's
# ``/api/embed`` -- and the local single-slot semaphore is taken only under
# ``if benchVersion < 7``. The memory phase mirrors it:
# ``beginMemoryPhase(..., localEmbedding: false)`` for v7, ``true`` for v2-v6.
# A v7 run therefore never opens a socket to this host's Ollama, and a dead
# Ollama says nothing about whether this validator can serve v7.
#
# The lane is NOT removed for v2-v6: those code paths still exist, and the local
# container is single-tenant per host, so the serialisation around it stays. This
# set is the one place that knows which versions need it -- a future v8 lands
# here or nowhere.
_LOCAL_EMBEDDING_BENCH_VERSIONS = frozenset({2, 3, 4, 5, 6})


@dataclass(frozen=True)
class LocalEmbeddingLane:
    """What this host's own Ollama container can currently support.

    A capability, not a health boolean: the question that decides whether to
    claim a ticket is "which benchmark versions can this validator still drive",
    and the local lane only ever answers it for
    :data:`_LOCAL_EMBEDDING_BENCH_VERSIONS`.

    ``unknown`` is the pre-observation state (and mock/plumbing mode). It is
    never read as broken -- absence of evidence is not evidence of a dead lane,
    and treating it as one would fail closed on every process start.
    """

    status: Literal["unknown", "healthy", "unavailable"] = "unknown"
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.status != "unavailable"

    def supports(self, bench_version: int) -> bool:
        """Whether this lane state permits driving ``bench_version``."""
        return self.usable or bench_version not in _LOCAL_EMBEDDING_BENCH_VERSIONS


# Scorer identity faults. Both degrade the validator to benchmark v2 and both
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

# The exact per-route identity the validator signs. The scorer may describe a
# route with more than this; anything outside these three fields is not part of
# the identity and must never decide whether v7 is advertised.
_ROUTE_IDENTITY_FIELDS = ("provider", "profile_revision", "model")


def _parse_v7_calibration(payload: object) -> V7InferenceCalibration | None:
    """Read the scorer's exact reviewed manifest identity, or fail closed.

    Only the fields the validator actually signs are read, and they are read by
    name. The scorer's capability document is a *superset* of the signed
    identity: it also carries descriptive metadata — ``provenance`` today — that
    has no field in the heartbeat model, and that model forbids extras because
    its bytes are signed.

    Copying the whole object into it therefore meant that any new descriptive
    key silently disabled v7 for the entire fleet. That is exactly what
    happened: the scorer began reporting ``"provenance": "reviewed-measured"``
    alongside a perfectly valid manifest, this returned ``None``, v7 was
    stripped from the advertisement, and the validator still reported the scorer
    ``fresh_verified`` and ``healthy`` because every identity field matched.

    Projection keeps the binding exact — an unparseable or incomplete identity
    still fails closed — while leaving the scorer free to describe itself.
    """
    if not isinstance(payload, dict):
        return None
    raw_routes = payload.get("supported_routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        return None
    if not all(isinstance(route, dict) for route in raw_routes):
        return None
    try:
        routes = tuple(
            sorted(
                (
                    InferenceCalibrationRoute.model_validate(
                        {field: route.get(field) for field in _ROUTE_IDENTITY_FIELDS}
                    )
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
            {
                "manifest_sha256": payload.get("manifest_sha256"),
                "supported_routes": routes,
            }
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
# ``inference_allowance_exhausted`` is what the scorer now emits when the
# harness spent the request-count or token allowance its own ticket granted, or
# sent a single request too large to reserve (platform decline codes
# 4102/4104/4109). The lease was alive and the platform healthy in every one of
# those; the agent simply spent what it was given.
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
_AGENT_ATTRIBUTABLE_INFERENCE_CODES = frozenset({"inference_allowance_exhausted"})

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
        # Why the scorer's running identity could not be verified, or ``None``
        # while it verifies. Read by telemetry and by the log de-duplication
        # below; the heartbeat carries the same finding as an
        # ``identity_mismatch`` scorer status.
        self.scorer_identity_fault: str | None = None
        self._scorer_identity_fault_logged: str | None = None
        # Whether the scorer offered v7 but its calibration identity could not
        # be read. Latched so the warning is one per transition, not one per
        # heartbeat, and cleared as soon as a readable calibration arrives.
        self._v7_calibration_rejected = False
        # Liveness evidence for the heartbeat: when the scorer last answered
        # with a capability document this validator could read whole, and how
        # many probes have failed to get one since. Process-local by design —
        # after a restart the validator knows nothing about the past and says
        # so, rather than carrying a claim it cannot support.
        self._scorer_last_served_at: int | None = None
        self._scorer_probe_failures = 0
        # What this host's own Ollama container can still support, as observed
        # by the most recent :meth:`preflight`. Read by the capability
        # advertisement (to stop offering versions this validator cannot drive)
        # and by :meth:`score_tarball` (to refuse such a ticket as
        # infrastructure rather than score it as a miner failure).
        self.local_embedding_lane = LocalEmbeddingLane()
        self._local_embedding_lane_logged: str | None = None
        # Benchmark versions this validator most recently advertised. Preflight
        # needs it to answer "is anything still servable without the local
        # lane"; empty means nothing has been observed yet, which fails closed.
        self._advertised_bench_versions: tuple[int, ...] = ()

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
                headers=self._control_headers(),
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
        capability = await self._observe_scorer_benchmark_capability(stack)
        self._advertised_bench_versions = tuple(capability.supported_bench_versions)
        return capability

    def _locally_servable(self, versions: tuple[int, ...]) -> tuple[int, ...]:
        """Drop versions this host's dead local embedding lane cannot drive.

        Returns ``versions`` unchanged when the lane is usable, or when dropping
        would leave nothing: an empty advertisement has no representation in
        :class:`ScorerBenchmarkCapability`, and inventing one (e.g. collapsing to
        ``unreachable``/``(2,)``) would blame the scorer for a fault that is not
        its own. That case is instead blocked one layer up, where
        :meth:`preflight` refuses to claim any ticket at all -- which is exactly
        the behaviour this whole change preserves for a v2-v6-only validator.
        """
        if self.local_embedding_lane.usable:
            return versions
        servable = tuple(
            version
            for version in versions
            if version not in _LOCAL_EMBEDDING_BENCH_VERSIONS
        )
        return servable or versions

    async def _observe_scorer_benchmark_capability(
        self, stack: ValidatorStackIdentity
    ) -> ScorerBenchmarkCapability:
        """Observe scorer support and bind post-v2 claims to signed stack identity.

        Legacy 404s, malformed replies, timeouts, and source mismatches all fail
        closed to v2. A newer scorer may advertise future benchmark versions;
        report only the intersection this validator knows how to drive so a
        forward-compatible rollout stays healthy without claiming unsupported
        work. Unknown versions at or below this validator's current maximum are
        still malformed and fail closed.

        The revision the scorer reports is only trustworthy when it is a
        property of the running binary. ``source_revision_origin`` says which it
        is, and ``source_revision_mismatch`` reports that the binary and the
        environment disagreed — the signature of a container recreated against a
        cached image. When the claim is merely environment-asserted, the
        capability set the scorer actually serves is the remaining evidence: a
        scorer that cannot advertise the version its pinned revision is known to
        support is not running that revision. Either way the result is a
        *reported* mismatch, never a crash: the validator keeps heartbeating,
        drops to v2, and shows up degraded instead of silently scoring on an old
        scorer.
        """
        observed_at = int(time.time())
        if self._config.dittobench_mock:
            # Local plumbing mode performs no observation. Reporting one would
            # be an invention, so it reports that none was made.
            return ScorerBenchmarkCapability(
                status="legacy_v2",
                supported_bench_versions=(2,),
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
                supported_bench_versions=(2,),
                probe=self._record_scorer_probe(
                    "timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "connect_error",
                    observed_at=observed_at,
                ),
            )
        if response.status_code == 404:
            # A genuine pre-capabilities scorer answers exactly this way, and so
            # does a sidecar that never mounted the route. The status stays
            # legacy_v2 -- fail closed either way -- but the probe now carries
            # the evidence that tells the two apart.
            return ScorerBenchmarkCapability(
                status="legacy_v2",
                supported_bench_versions=(2,),
                probe=self._record_scorer_probe(
                    "http_error", observed_at=observed_at, http_status=404
                ),
            )
        if response.status_code != 200:
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(2,),
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
                supported_bench_versions=(2,),
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
                supported_bench_versions=(2,),
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
            or type(full_run_capacity) is not int
            or not 1 <= full_run_capacity <= 8
        ):
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(2,),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="malformed_capabilities",
                ),
            )
        advertised_versions = tuple(sorted(set(versions)))
        if any(
            version not in _SUPPORTED_BENCH_VERSIONS
            and version <= _SUPPORTED_BENCH_VERSIONS[-1]
            for version in advertised_versions
        ):
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(2,),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="unsupported_bench_version",
                ),
            )
        observed_versions = tuple(
            version
            for version in advertised_versions
            if version in _SUPPORTED_BENCH_VERSIONS
        )
        if not observed_versions:
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(2,),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="unsupported_bench_version",
                ),
            )
        # Set when the reply was readable but this validator had to narrow it.
        # The advertisement below then reports less than the scorer offered, and
        # the probe says so instead of reading as a clean success.
        narrowed: ScorerProbeReason | None = None
        v7_calibration = _parse_v7_calibration(payload.get("v7_calibration"))
        if 7 in observed_versions and v7_calibration is None:
            # Dropping v7 is the correct fail-closed action, but doing it
            # quietly is how a whole fleet stopped advertising the active bench
            # while every other signal stayed green. Say so, once per
            # transition, and name the keys we actually saw.
            self._log_v7_calibration_rejected(payload.get("v7_calibration"))
            narrowed = "calibration_unreadable"
            observed_versions = tuple(
                version for version in observed_versions if version != 7
            )
            if not observed_versions:
                return ScorerBenchmarkCapability(
                    status="unreachable",
                    supported_bench_versions=(2,),
                    probe=self._record_scorer_probe(
                        "unreadable",
                        observed_at=observed_at,
                        http_status=200,
                        reason="calibration_unreadable",
                    ),
                )
        elif 7 not in observed_versions:
            v7_calibration = None
        else:
            self._v7_calibration_rejected = False
        observed_versions = self._locally_servable(observed_versions)
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
                supported_bench_versions=(2,),
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
                v7_calibration=v7_calibration,
                probe=self._record_scorer_probe(
                    "served_degraded" if narrowed else "served",
                    observed_at=observed_at,
                    http_status=200,
                    reason=narrowed,
                ),
            )
        except ValueError:
            # Defensive: every constraint above is already satisfied here. If a
            # future one is not, the reply was not usable after all, so report
            # that rather than a capability nothing verified.
            return ScorerBenchmarkCapability(
                status="unreachable",
                supported_bench_versions=(2,),
                probe=self._record_scorer_probe(
                    "unreadable",
                    observed_at=observed_at,
                    http_status=200,
                    reason="malformed_capabilities",
                ),
            )

    def _log_v7_calibration_rejected(self, calibration: object) -> None:
        """Report that the scorer offered v7 but its identity was unreadable.

        Only field *names* are logged. The values are public identity, but the
        names are what an operator needs to see a shape drift between the
        scorer and this validator, which is the failure this exists to catch.
        """
        if self._v7_calibration_rejected:
            return
        self._v7_calibration_rejected = True
        keys = sorted(calibration) if isinstance(calibration, dict) else None
        logger.error(
            "scorer advertises benchmark v7 but its calibration identity could "
            "not be read; v7 will NOT be advertised (calibration fields=%s, "
            "expected exactly manifest_sha256 + supported_routes with "
            "provider/profile_revision/model). This validator cannot take v7 "
            "work until the shapes agree.",
            keys,
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
            "is degraded and will advertise benchmark v2 only until the scorer "
            "image is rebuilt from the pin; restarting the validator will not "
            "help. See docs/VALIDATOR.md, 'Stale scorer image'.",
            fault,
            source_revision,
            origin if isinstance(origin, str) else "unstamped",
            expected_revision,
            software_version,
            list(observed_versions),
        )

    async def preflight(self) -> None:
        """Observe the local embedding lane, and block the sweep only if it matters.

        The configured URL is the sandbox-docker forwarder's port 11434, the
        same listener reached as ``host.docker.internal:11434`` by inner miner
        harnesses. A real embedding request checks the forwarder, Ollama, and
        the loaded model rather than merely checking container liveness.

        This used to raise on any failure, which blocked *every* ticket claim on
        a dependency only :data:`_LOCAL_EMBEDDING_BENCH_VERSIONS` actually use.
        A host whose Ollama died was excluded from v7 -- the only version being
        scored -- for a container v7 never opens a socket to.

        So the failure is now recorded as a *capability* rather than raised as a
        fault: the lane result narrows what this validator advertises, and the
        sweep is blocked only when nothing servable is left. The observation
        itself is unchanged, and still reaches operators through the ``ollama``
        component of the signed stack health, which stays ``required`` because a
        validator that can no longer drive v2-v6 genuinely is degraded.
        """
        if self._config.dittobench_mock:
            return
        detail = await self._probe_local_embedding_lane()
        self._record_local_embedding_lane(detail)
        if detail is None:
            return
        hosted = tuple(
            version
            for version in self._advertised_bench_versions
            if version not in _LOCAL_EMBEDDING_BENCH_VERSIONS
        )
        if not hosted:
            # Either nothing has been advertised yet, or everything advertised
            # needs the lane. Both are exactly the old behaviour: claim nothing.
            raise ValidatorInfrastructureError(
                f"{detail}; this validator advertises no benchmark version that "
                "can be served without the local embedding lane"
            )
        # The locked model is now ticket-scoped, so it cannot be probed before
        # the platform issues and the local broker activates a capability. Each
        # scored run performs that trusted probe after activation instead.

    async def _probe_local_embedding_lane(self) -> str | None:
        """Return why the local lane is unusable, or ``None`` if it served."""
        try:
            response = await self._client.post(
                self._config.embed_preflight_url,
                json={"model": "embeddinggemma", "input": "validator preflight"},
                timeout=self._config.embed_preflight_timeout_seconds,
            )
        except httpx.TimeoutException:
            return "embedding preflight timed out through the harness forwarder"
        except httpx.HTTPError as e:
            return f"embedding preflight could not reach the harness forwarder: {e}"
        if response.status_code != 200:
            return (
                "embedding preflight through the harness forwarder rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            embeddings = response.json().get("embeddings")
        except (ValueError, AttributeError):
            return "embedding preflight returned an invalid response"
        if (
            not isinstance(embeddings, list)
            or not embeddings
            or not isinstance(embeddings[0], list)
            or not embeddings[0]
        ):
            return "embedding preflight returned no embedding vector"
        return None

    def _record_local_embedding_lane(self, detail: str | None) -> None:
        """Latch the lane state and log each transition exactly once."""
        self.local_embedding_lane = (
            LocalEmbeddingLane(status="healthy")
            if detail is None
            else LocalEmbeddingLane(status="unavailable", detail=detail)
        )
        status = self.local_embedding_lane.status
        if self._local_embedding_lane_logged == status:
            return
        self._local_embedding_lane_logged = status
        if detail is None:
            logger.info(
                "local embedding lane is serving again; benchmark versions %s "
                "are servable on this host once more",
                sorted(_LOCAL_EMBEDDING_BENCH_VERSIONS),
            )
            return
        logger.warning(
            "local embedding lane is unavailable (%s). Benchmark versions %s "
            "need it and will no longer be advertised by this validator; "
            "hosted-lane versions are unaffected and keep claiming tickets.",
            detail,
            sorted(_LOCAL_EMBEDDING_BENCH_VERSIONS),
        )

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
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        """Score a submission by its presigned tarball URL (mode B).

        Submits the scoring inputs, then polls until the run finishes.
        Raises :class:`DittobenchError` on a failed run or the overall timeout.

        ``ticket_deadline`` is the lease this run is being scored under. It
        bounds the poll independently of ``inference_ticket_deadline``, which
        only exists for v7 and only travels to the inference broker: every
        benchmark version has a lease to respect, so the abort must not depend
        on whether a broker session was involved.

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
        if not self.local_embedding_lane.supports(bench_version):
            # The narrowed advertisement should already have stopped this ticket
            # from being issued, but the platform may still hold a heartbeat from
            # before the lane died. Refuse it as *infrastructure* so the ticket
            # is returned and retried elsewhere -- scoring it would fail on this
            # host's dead container and charge the miner for it.
            raise ValidatorInfrastructureError(
                f"benchmark v{bench_version} needs the local embedding lane and "
                f"it is unavailable ({self.local_embedding_lane.detail})"
            )
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
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        if (
            expected_bench_version is None
            or expected_bench_version not in _SUPPORTED_BENCH_VERSIONS
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
                        raise ValidatorInfrastructureError(
                            f"run {run_id} reported validator infrastructure "
                            f"failure: {infrastructure_code}",
                            code=infrastructure_code,
                        )
                    if _is_embedding_infrastructure_failure(error):
                        raise ValidatorInfrastructureError(
                            f"run {run_id} lost validator embedding infrastructure: "
                            f"{error}",
                            code="embedding_provider_unavailable",
                        )
                    raise DittobenchError(f"run {run_id} failed: {error}")
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
