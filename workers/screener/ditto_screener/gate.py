"""Stable v6 screening core plus an optional private policy boundary.

The gate is deliberately cheaper than a full DittoBench run. It verifies the
image and service contract before a submission can consume a scoring run.

Flow for one agent:

1. **Download + verify.** Stream the presigned tarball to a temp file, bounded by
   ``max_tarball_bytes``, and re-check its SHA-256 against the queue value (the
   URL is presigned but the bytes are still attacker-controlled).
2. **Contract check.** Reject unsafe archive entries and require a root
   ``Dockerfile`` before any build is attempted. The implementation language is
   deliberately unconstrained; the image must satisfy the HTTP harness contract.
3. **Build.** Prefer the attempt-bound Targon Kaniko archive. When its runtime
   smoke already succeeded, the worker never docker-loads or rebuilds. Local
   ``docker build`` is residual fallback for ``prefer``/``off`` only.
4. **Serve smoke.** Reuse the Targon rental ``GET /health`` when that lane
   succeeded. Otherwise run the image detached with a memory + pids cap and
   poll ``GET /health`` until it returns 2xx.
5. **Private policy.** The default v8 manifest performs bounded Luna source
   review after health. A rotating
   private manifest may use timing, random-control, fingerprint, and behavioral
   audit modules. Those signals can only pass or route to review; they cannot
   produce a deterministic rejection.
6. **Teardown.** The container + image are always removed.

A pass is "built, served, and cleared by bounded source review" under the
default production-v8 manifest.
Deterministic contract violations fail; infrastructure failures are reported
separately so Platform can park them for an operator-issued retry.
Failures include a short ``detail``
(response body, container-log tail, or failing stage) for the miner and operator.
Every stage is best-effort and never raises into the worker loop: an
infrastructure error (Docker down) is reported as a non-pass with detail, so a
flaky host does not silently promote or wrongly reject.

Trust posture: the Docker endpoint is operator-owned and may be required to be
rootless by deployment policy. Build and runtime wall-time and resources are
bounded; no submission-controlled credential is mounted into either boundary.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import signal
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from uuid import UUID

import httpx

from ditto_screener.adjudicator import build_adjudicator
from ditto_screener.fake_gateway import LOCKED_HARNESS_MODEL
from ditto_screener.heartbeat import (
    ScreenerProgressStage,
    source_review_progress_stage,
)
from ditto_screener.l2_review import (
    IsolatedCodingHarness,
    L2AuditJournal,
    L2RunResult,
    LayeredSourceReviewAgent,
    TerraSolSourceReviewAgent,
)
from ditto_screener.platform import (
    LocalScreeningProviderSelected,
    RemoteSubmissionBuildRejected,
)
from ditto_screener.policy import (
    ChallengeObservation,
    PolicyContext,
    PolicyEngine,
    PolicyEvidence,
    ReviewJournal,
    ScreeningDecision,
    ScreeningOutcome,
    core_decision,
    load_policy_engine,
)
from ditto_screener.preflight_audit import (
    StaticPreflightAuditError,
    StaticPreflightAuditJournal,
)
from ditto_screener.source_review import (
    OpenRouterSourceReviewAgent,
    SourceReviewObservation,
    TarSourceRepository,
)
from ditto_screening_protocol import SCREENING_POLICY_VERSION

if TYPE_CHECKING:
    from ditto_screener.config import ScreenerConfig
    from ditto_screener.platform import RemoteImageArchive
    from ditto_screener.review_settings import EffectiveReviewSettings

logger = logging.getLogger(__name__)

# Bytes of a failing build log to attach to the verdict detail.
_LOG_TAIL_BYTES = 2000
_MAX_GATE_DETAIL_CHARS = 3900
# How long to wait between /health probes while the container boots.
_PROBE_INTERVAL_SECONDS = 1.0
# Refuse to begin a screening stage that cannot plausibly finish and still leave
# the worker time to sign and post a verdict before the lease deadline. A stage
# entered with less than this many seconds of lease budget is abandoned as an
# infrastructure failure so Platform can park it with exact evidence.
_LEASE_MIN_STAGE_SECONDS = 5.0
_MAX_UNPACKED_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_SCREENED_IMAGE_BYTES = 8 * 1024**3
_IMAGE_EXPORT_DISK_RESERVE_BYTES = 256 * 1024**2
_IMAGE_HASH_CHUNK_BYTES = 8 * 1024**2
_MAX_CANARY_RESPONSE_BYTES = 64 * 1024
_CANARY_IMAGE = (
    "python:3.12-alpine@sha256:"
    "6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
)
_GATEWAY_ALIAS = "host.docker.internal"
_CHAT_GATEWAY_PORT = 11435
_EMBED_GATEWAY_PORT = 11434
_HARNESS_ALIAS = "harness"
_VALIDATOR_SANDBOX_USER = "65532:65532"
_VALIDATOR_SANDBOX_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=512m"
_VALIDATOR_SANDBOX_MEMORY = "3g"
_VALIDATOR_SANDBOX_CPUS = "2"
_VALIDATOR_SANDBOX_PIDS = "512"
_VALIDATOR_SANDBOX_DB = "/tmp/dittobench.db"
_DOCKER_INFRASTRUCTURE_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "docker daemon is not running",
    "docker image inspect failed",
    "connection refused",
    "no space left on device",
    "out of memory",
    "cannot allocate memory",
    "killed",
    "docker command exited with signal",
    "signal sigterm",
    "signal sigkill",
    # Common cgroup / compiler spellings do not include the whitespace-only
    # form above (for example rustc reports ``signal: 9, SIGKILL: kill``).
    "sigkill",
    "oomkilled",
    "memory cgroup out of memory",
    "exit code: 137",
    # A build the daemon or worker was restarted out from under (deploy /
    # `systemctl restart docker`) aborts with BuildKit's cancellation marker.
    # That is our own interruption, never the miner's crate failing to compile,
    # so it is reported as infrastructure rather than rejecting the artifact.
    "context canceled",
    "context cancelled",
    "buildkit",
    "snapshotter",
    "failed to mount",
    "failed to lchown",
    "lchownat",
    "secret gh_token",
    "secret id=gh_token",
    "temporary failure in name resolution",
    "could not resolve host",
    "tls handshake timeout",
    "i/o timeout",
    "connection reset by peer",
    "unexpected eof",
    "too many requests",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


@dataclass(frozen=True)
class _StageResult:
    """Internal stable-core stage result."""

    passed: bool
    detail: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.passed and self.retryable:
            raise ValueError("a passing stage result cannot be retryable")


class LeaseDeadline(float):
    """Mutable monotonic deadline shared with the heartbeat renewal task."""

    expires_at: float

    def __new__(cls, expires_at: float) -> LeaseDeadline:
        instance = super().__new__(cls, expires_at)
        instance.expires_at = expires_at
        return instance

    def renew(self, expires_at: float) -> None:
        self.expires_at = max(self.expires_at, expires_at)

    def __sub__(self, other: object) -> float:
        if not isinstance(other, int | float):
            return NotImplemented
        return self.expires_at - other

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, int | float):
            return NotImplemented
        return self.expires_at < other

    def __le__(self, other: object) -> bool:
        if not isinstance(other, int | float):
            return NotImplemented
        return self.expires_at <= other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, int | float):
            return NotImplemented
        return self.expires_at > other

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, int | float):
            return NotImplemented
        return self.expires_at >= other


Deadline = float | None


@dataclass(frozen=True)
class BuiltImageArtifact:
    """A locally exported, content-addressed Docker image archive."""

    path: str
    sha256: str
    size_bytes: int
    image_id: str
    image_ref: str


@dataclass(frozen=True)
class _PortableImageArchive:
    """Classic single-image transport plus its config-digest identity."""

    path: str
    image_id: str


class _ScreenedImageTooLargeError(ValueError):
    """The miner-controlled image deterministically exceeds the archive cap."""


class _ScreenedImageExportError(RuntimeError):
    """The host could not export an otherwise passing screened image."""


class _LeaseDeadlineError(TimeoutError):
    """An image export/publication operation exhausted the screening lease."""


@dataclass(frozen=True)
class _AuditRuntime:
    """Ephemeral values used only while a selected private audit runs."""

    harness_base: str
    gateway_response_token: str
    oracle_answer: str
    gateway_state_file: str


# The fake gateway serves a benign `/tool` sink at the same host-container alias
# the harness already uses for the model, so a tool-shaped challenge's
# `tool_endpoint` is reachable from inside the harness network and carries no
# screener-specific tell (it is the same host:port the model calls go to).
_TOOL_ENDPOINT = f"http://{_GATEWAY_ALIAS}:{_CHAT_GATEWAY_PORT}/tool"


def _with_tool_endpoint(request: Mapping[str, object]) -> dict[str, object]:
    """Fill a reachable ``tool_endpoint`` for a tool-declaring challenge request.

    Returns a copy so the caller's mapping is not mutated. A request that
    already carries a ``tool_endpoint``, or declares no ``tools``, is returned
    unchanged (aside from the copy).

    This applies to any tool-declaring private challenge, not only the oracle.
    An explicit ``tool_endpoint`` is always preserved, so a challenge pack that
    deliberately wants a different endpoint — including an unreachable one, to
    observe whether the harness fabricates tool results with no live endpoint —
    sets its own and is never overridden by the gateway sink.
    """
    payload = dict(request)
    if payload.get("tools") and not payload.get("tool_endpoint"):
        payload["tool_endpoint"] = _TOOL_ENDPOINT
    return payload


def dockerfile_at_root(member_names: list[str]) -> bool:
    """Whether the tar has a ``Dockerfile`` at its root.

    Accepts the bare ``Dockerfile`` and a leading ``./`` (tar writers differ).
    The submission contract fixes the Dockerfile at the tarball root, so a
    Dockerfile only in a subdirectory does not satisfy the gate.
    """
    return any(name in ("Dockerfile", "./Dockerfile") for name in member_names)


def _contract_diagnostic(code: str, message: str, help_text: str) -> str:
    """Return a stable contract diagnostic without source excerpts."""
    return f"error[{code}]: {message}\n\nhelp: {help_text}"


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """Parse a Dockerfile into (INSTRUCTION, remainder) pairs.

    Line continuations are joined and standalone comment lines are dropped.
    This is a bounded static parse, not a full Dockerfile grammar.
    """
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        buffer = f"{buffer} {stripped}".strip() if buffer else stripped
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        logical.append(buffer)
        buffer = ""
    if buffer:
        logical.append(buffer)
    instructions: list[tuple[str, str]] = []
    for line in logical:
        parts = line.split(None, 1)
        if parts:
            instructions.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return instructions


def image_binding_advisory(dockerfile_text: str) -> str | None:
    """Flag an entrypoint image with no visible build-context provenance.

    This is a bounded static text heuristic, so it is ADVISORY ONLY and routes
    to operator-reviewed quarantine, never to a deterministic rejection: a
    legitimate image can construct its runtime in a helper or package-manager
    step that a text parser cannot understand. Conversely, merely naming a
    compiler proves nothing. The language-neutral signal is whether the image
    declares a runnable entrypoint but never copies any reviewed build-context
    bytes into any stage.
    """
    has_context_copy = False
    has_build_step = False
    entrypoints: list[str] = []
    for keyword, rest in _dockerfile_instructions(dockerfile_text):
        if keyword in {"COPY", "ADD"} and "--from=" not in rest.casefold():
            has_context_copy = True
        elif keyword == "RUN":
            has_build_step = True
        elif keyword in {"ENTRYPOINT", "CMD"}:
            entrypoints.append(rest)
    if entrypoints and not has_context_copy:
        return (
            "Dockerfile sets an entrypoint without copying reviewed build-context "
            "files; the running image may not be the reviewed source"
        )
    if entrypoints and has_context_copy and not has_build_step:
        joined = " ".join(entrypoints).casefold()
        transparent_runtime = re.search(
            r"\b(?:python\d*|node|deno|bun|ruby|php|java|dotnet|elixir|erl|lua|"
            r"swift|bash|sh|npm|pnpm|yarn)\b|"
            r"\.(?:py|js|mjs|cjs|ts|tsx|rb|php|jar|war|dll|exs?|erl|lua|sh)\b",
            joined,
        )
        if transparent_runtime is None:
            return (
                "Dockerfile copies an opaque entrypoint without a visible build "
                "step; the running image may not be the reviewed source"
            )
    return None


def _with_image_binding_advisory(
    decision: ScreeningDecision, advisory: str | None
) -> ScreeningDecision:
    """Escalate a passing decision to operator review on a provenance warning.

    The heuristic is text matching, so it can neither prove nor disprove that
    the image runs the reviewed source. It therefore never rejects: a PASS
    becomes an operator-reviewed QUARANTINE and an existing QUARANTINE gains
    the evidence item; terminal rejections and parked infrastructure failures are
    untouched.
    """
    if advisory is None or decision.outcome not in {
        ScreeningOutcome.PASS,
        ScreeningOutcome.PASS_INCONCLUSIVE,
        ScreeningOutcome.QUARANTINE,
    }:
        return decision
    evidence = (
        *decision.evidence[:15],
        PolicyEvidence("stable-core", "image-binding-heuristic", advisory[:240]),
    )
    return ScreeningDecision(
        outcome=ScreeningOutcome.QUARANTINE,
        detail="private policy quarantine pending operator review",
        manifest_digest=decision.manifest_digest,
        evidence=evidence,
        finding=decision.finding,
        review_audit=decision.review_audit,
    )


def _gateway_call_count(path: str) -> int:
    """Count bounded call markers written by one isolated fake gateway."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return 0
    if len(data) > 64 * 1024:
        raise ValueError("fake gateway call state exceeded safety cap")
    return data.count(b"1\n")


def _prepare_gateway_state() -> tuple[str, str]:
    """Stage host-visible fake-gateway inputs and its writable call counter.

    Root in a rootless Docker user namespace maps to a different host uid.  The
    worker therefore owns the pre-created file and grants that mapped uid only
    append access.  The containing directory is searchable but not writable, so
    the gateway cannot replace the counter with a file the worker cannot read.
    """
    # A systemd worker may use PrivateTmp, but rootless Docker runs outside that
    # namespace. A bind mount from the private /tmp is therefore invisible to
    # the daemon. The fleet gives us an explicit, per-worker host-visible root
    # for these ephemeral inputs; local/test workers retain tempfile's default
    # when it is not configured.  Stage the static gateway script here too:
    # ProtectSystem=strict makes the immutable release path an invalid Docker
    # bind-mount source for the rootless daemon.
    shared_root = os.environ.get("SCREENER_GATEWAY_STATE_ROOT")
    if shared_root:
        Path(shared_root).mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir = tempfile.mkdtemp(prefix="ditto-gateway-state-", dir=shared_root)
    state_file = str(Path(state_dir) / "model-called")
    try:
        staged_script = Path(state_dir) / "fake_gateway.py"
        shutil.copyfile(Path(__file__).with_name("fake_gateway.py"), staged_script)
        os.chmod(staged_script, 0o444)
        os.chmod(state_dir, 0o711)
        fd = os.open(state_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(state_file, 0o622)
    except Exception:
        shutil.rmtree(state_dir, ignore_errors=True)
        raise
    return state_dir, state_file


def _contains_string(value: object, needle: str) -> bool:
    """Whether a JSON value contains the exact ephemeral gateway token."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, list):
        return any(_contains_string(item, needle) for item in value)
    if isinstance(value, dict):
        return any(_contains_string(item, needle) for item in value.values())
    return False


def _log_tail(text: str) -> str:
    """Last chunk of a build log, trimmed for the verdict detail field."""
    trimmed = text.strip()
    if len(trimmed) <= _LOG_TAIL_BYTES:
        return trimmed
    return "…" + trimmed[-_LOG_TAIL_BYTES:]


def _detail_tail(text: str) -> str:
    """Keep a result detail below the shared protocol's 4,000-char cap."""
    trimmed = text.strip()
    if len(trimmed) <= _MAX_GATE_DETAIL_CHARS:
        return trimmed
    return "…" + trimmed[-(_MAX_GATE_DETAIL_CHARS - 1) :]


def _docker_infrastructure_failure(text: str) -> bool:
    normalized = text.casefold()
    return any(marker in normalized for marker in _DOCKER_INFRASTRUCTURE_MARKERS)


@contextlib.contextmanager
def _normalized_build_context(tar_path: str) -> Iterator[io.BufferedReader]:
    """Yield the validated archive with portable ownership metadata.

    Miner archives can legitimately originate inside a user namespace and carry
    UID/GID values that the screener host cannot represent. BuildKit attempts to
    apply those IDs while loading stdin and fails before reading the Dockerfile.
    Re-streaming regular files and directories keeps the submitted bytes intact
    while making the transport metadata portable. ``_contract_error`` has
    already rejected links, devices, aliases, duplicates, and unsafe paths.
    """
    fd, normalized_path = tempfile.mkstemp(prefix="ditto-build-context-", suffix=".tgz")
    try:
        with (
            os.fdopen(fd, "wb") as normalized,
            tarfile.open(tar_path, mode="r:gz") as source,
            tarfile.open(fileobj=normalized, mode="w:gz") as destination,
        ):
            for member in source:
                name = member.name.removeprefix("./")
                if not name and member.isdir():
                    continue
                portable = tarfile.TarInfo(name=name)
                portable.type = member.type
                portable.mode = member.mode & 0o7777
                portable.mtime = member.mtime
                portable.uid = 0
                portable.gid = 0
                portable.uname = ""
                portable.gname = ""
                if member.isfile():
                    portable.size = member.size
                    payload = source.extractfile(member)
                    if payload is None:
                        raise tarfile.ReadError(
                            f"validated regular file could not be read: {name}"
                        )
                    destination.addfile(portable, payload)
                else:
                    portable.size = 0
                    destination.addfile(portable)
        with open(normalized_path, "rb") as normalized_input:
            yield normalized_input
    finally:
        with contextlib.suppress(OSError):
            os.unlink(normalized_path)


def _format_stage_timings(history: Sequence[tuple[str, float]], *, end: float) -> str:
    """Fold progress transitions into ``stage=<ms>`` pairs, in order.

    Each stage's duration runs until the next transition (the last until
    ``end``). The per-percent ``source_review_NN`` stages collapse into one
    ``source_review`` bucket, and a revisited stage name accumulates.
    """
    durations: dict[str, int] = {}
    for index, (stage, entered) in enumerate(history):
        exited = history[index + 1][1] if index + 1 < len(history) else end
        name = "source_review" if stage.startswith("source_review_") else str(stage)
        durations[name] = durations.get(name, 0) + round(
            max(0.0, exited - entered) * 1000
        )
    return " ".join(f"{name}_ms={ms}" for name, ms in durations.items())


class BuildGate:
    """Runs the build, serve, and model-call checks for one agent at a time.

    Docker CLI calls are funnelled through :meth:`_run` so tests can stub the
    subprocess layer; HTTP (download + health probe) uses the injected client.
    """

    def __init__(
        self,
        config: ScreenerConfig,
        client: httpx.AsyncClient,
        *,
        policy: PolicyEngine,
        journal: ReviewJournal,
    ) -> None:
        self._config = config
        self._client = client
        self._policy = policy
        self._journal = journal
        self._static_preflight_audit = StaticPreflightAuditJournal(
            config.static_preflight_audit_file
        )
        self._review_settings_key: tuple[int, str] | None = None
        self._executor_verified = False
        self._configure_source_reviewer(config)

    def _configure_source_reviewer(self, config: ScreenerConfig) -> None:
        l1_reviewer = OpenRouterSourceReviewAgent(
            api_key_file=config.source_review_api_key_file,
            model=config.source_review_model,
            base_url=config.source_review_base_url,
            timeout_seconds=config.source_review_timeout_seconds,
            max_steps=config.source_review_max_steps,
            max_read_bytes=config.source_review_max_read_bytes,
            max_completion_tokens=config.source_review_max_completion_tokens,
            reasoning_effort=config.source_review_reasoning_effort,
            static_preflight_v2_mode=config.static_preflight_v2_mode,
            concern_hold_count=config.review_concern_hold_count,
            clear_min_notes=config.review_clear_min_notes,
        )
        l2_reviewer = TerraSolSourceReviewAgent(
            api_key_file=config.source_review_api_key_file,
            base_url=config.source_review_base_url,
            harness=IsolatedCodingHarness(
                docker_bin=config.docker_bin,
                image=config.l2_analyzer_image,
                rootless_docker_host=(
                    config.docker_host if config.require_rootless_docker else None
                ),
            ),
            workspace_root=config.l2_workspace_root,
            cache_dir=config.l2_cache_dir,
            audit_journal=L2AuditJournal(
                config.l2_audit_journal_file,
                retention_days=config.l2_audit_retention_days,
            ),
            timeout_seconds=config.l2_timeout_seconds,
            max_steps=config.l2_max_steps,
            max_input_tokens=config.l2_max_input_tokens,
            max_output_tokens=config.l2_max_output_tokens,
            max_completion_tokens=config.l2_max_completion_tokens,
            max_cost_usd=config.l2_max_cost_usd,
            analyst_reasoning_effort=config.l2_analyst_reasoning_effort,
            critic_reasoning_effort=config.l2_critic_reasoning_effort,
            cache_ttl_seconds=config.l2_cache_ttl_seconds,
            model=config.l2_review_model,
            fallback_models=config.l2_fallback_models,
            l3_enabled=config.l3_review_enabled,
            critic_model=config.l3_review_model,
            critic_provider=config.l3_review_provider,
        )
        self._source_reviewer = LayeredSourceReviewAgent(
            l1=l1_reviewer,
            l2=l2_reviewer,
            mode=config.l2_review_mode,
            concern_hold_count=config.review_concern_hold_count,
            clear_min_notes=config.review_clear_min_notes,
            adjudicator=build_adjudicator(config),
            adjudicator_reserve_seconds=config.adjudicator_timeout_seconds,
        )

    def apply_review_settings(self, effective: EffectiveReviewSettings) -> bool:
        """Apply one validated revision between leases; return whether it changed."""
        key = (effective.revision, effective.checksum)
        if key == self._review_settings_key:
            return False
        runtime = effective.apply_to(self._config)
        self._policy = load_policy_engine(
            runtime.policy_manifest_file,
            l2_mode=runtime.l2_review_mode,
            manifest_profile=effective.settings.policy_manifest_profile,
            rotation_id=effective.settings.policy_manifest_rotation_id,
        )
        self._configure_source_reviewer(runtime)
        self._review_settings_key = key
        logger.info(
            "applied review settings revision=%d scope=%s mode=%s model=%s "
            "l3_enabled=%s",
            effective.revision,
            effective.scope,
            runtime.l2_review_mode,
            runtime.l2_review_model,
            runtime.l3_review_enabled,
        )
        return True

    def pop_shadow_review(self, attempt_id: UUID) -> L2RunResult | None:
        """Return and remove one attempt's non-authoritative shadow result."""
        return self._source_reviewer.pop_shadow_result(attempt_id)

    async def screen(
        self,
        *,
        agent_id: UUID,
        attempt_id: UUID,
        bench_version: int,
        miner_hotkey: str,
        sha256: str,
        download_url: str,
        progress: Callable[[ScreenerProgressStage], None] | None = None,
        deadline: Deadline = None,
        publish_image: Callable[[BuiltImageArtifact], Awaitable[None]] | None = None,
        remote_build: Callable[[], Awaitable[RemoteImageArchive | None]] | None = None,
        remote_build_consumed: Callable[[UUID], Awaitable[None]] | None = None,
        remote_source_review: Callable[[], Awaitable[SourceReviewObservation | None]]
        | None = None,
        build_only: bool = False,
        policy_only: bool = False,
        deferred_source_review: bool = False,
        policy_version: int = SCREENING_POLICY_VERSION,
    ) -> ScreeningDecision:
        """Screen one agent end-to-end; never raises.

        ``bench_version`` is the exact generation Platform assigned to this
        submission. Private behavioral challenges reuse it so the challenge
        envelope cannot drift from the scored request contract.

        ``deadline`` is an optional monotonic-clock (``loop.time()``) bound for
        the whole screen, derived by the worker from the platform's lease. When
        set, each heavy stage is clamped to the remaining budget and refuses to
        start once the budget is spent, so a slow build or source review can no
        longer run past the lease and have its verdict rejected as expired.

        ``build_only`` selects the mechanical lane. It is used for both an
        already-adjudicated prerequisite rebuild and score-first admission
        whose deep source review is deferred. It skips source review but still
        performs archive/contract validation, build, serve, isolation, and
        image-export work. A mechanical screen can only pass, report a genuine
        deterministic or infrastructure failure in those stages, or run out of
        lease budget; private-policy checks run in the later full review.

        ``deferred_source_review`` distinguishes a fresh score-first admission
        from an already-adjudicated rebuild for the signed platform contract.
        Both mechanical paths skip private-policy work here.

        ``policy_only`` selects a stale-policy rescreen whose previously
        verified image and runtime smoke are retained by Platform. It reruns
        archive/source policy checks without rebuilding, serving, or exporting.
        """

        if build_only and policy_only:
            raise ValueError("build-only and policy-only modes are mutually exclusive")

        loop = asyncio.get_running_loop()
        screen_started = loop.time()
        # (stage, entered_at) transitions; folded into one per-stage timing
        # log line when the screen ends, so operators can see where each
        # screening spent its wall clock without any external tooling.
        stage_history: list[tuple[str, float]] = []

        def report(stage: ScreenerProgressStage) -> None:
            stage_history.append((stage, loop.time()))
            try:
                if progress is not None:
                    progress(stage)
            except Exception:  # noqa: BLE001 - telemetry cannot affect screening
                logger.warning("screener progress callback failed; screening continues")

        # Attempt identity prevents stale runtime/build resources from a
        # crashed or reissued ticket from colliding with its replacement.  The
        # published image reference remains stable for the immutable agent
        # submission; downstream consumers and rescreens share that identity.
        execution_id = f"{agent_id}-{attempt_id}"
        build_tag = f"ditto-screen/{execution_id}:latest"
        image_ref = f"ditto-screen/{agent_id}:latest"
        container = f"ditto-screen-{execution_id}"
        gateway_container = f"ditto-gateway-{execution_id}"
        network = f"ditto-screen-{execution_id}"
        gateway_state_dir, _ = _prepare_gateway_state()
        tmp_path: str | None = None
        review_task: asyncio.Task[SourceReviewObservation] | None = None
        review_factory: (
            Callable[[], Coroutine[Any, Any, SourceReviewObservation]] | None
        ) = None
        used_local_docker = False
        remote_archive: RemoteImageArchive | None = None
        try:
            report("downloading")
            if (exhausted := self._lease_exhausted(deadline, "download")) is not None:
                return exhausted
            tmp_path, dl_detail = await self._download_verified(download_url, sha256)
            if tmp_path is None:
                outcome = (
                    ScreeningOutcome.RETRYABLE_INFRA
                    if dl_detail.startswith("artifact download")
                    else ScreeningOutcome.DETERMINISTIC_REJECT
                )
                detail = (
                    f"screener error: {dl_detail}"
                    if outcome == ScreeningOutcome.RETRYABLE_INFRA
                    else dl_detail
                )
                return core_decision(
                    outcome,
                    code="artifact-download"
                    if outcome == ScreeningOutcome.RETRYABLE_INFRA
                    else "artifact-invalid",
                    summary="artifact download infrastructure failed"
                    if outcome == ScreeningOutcome.RETRYABLE_INFRA
                    else "artifact violated the bounded download contract",
                    detail=detail,
                )
            report("validating")
            contract_error = self._contract_error(tmp_path)
            if contract_error is not None:
                return core_decision(
                    ScreeningOutcome.DETERMINISTIC_REJECT,
                    code="container-harness-contract",
                    summary="artifact does not satisfy the container harness contract",
                    detail=contract_error,
                )
            source_digest, source_paths = self._source_metadata(tmp_path)

            # General source review is deliberately deferred until the image
            # has built and passed its runtime contract. Broken Dockerfiles and
            # unhealthy containers should not consume model-review capacity.
            # The static preflight below remains before build because it is the
            # safety boundary for submission-controlled Docker execution.
            in_policy_phase = False

            def report_review_progress(completed: int, total: int) -> None:
                if in_policy_phase:
                    report(source_review_progress_stage(completed, total))

            preflight_clearance: SourceReviewObservation | None = None

            # The mechanical lane deliberately skips source / pre-execution
            # review (the static lead and agentic reviewer): no lead is
            # resolved, no reviewer is launched, and the policy receives no
            # source-review callback below. Archive, build, runtime-health,
            # isolation, and export gates remain authoritative.
            if not build_only:
                # Static rules run before any submission-controlled Dockerfile or
                # image, but they are routing leads rather than proof. Resolve an
                # elevated lead with the inert L2/L3 harness before deciding
                # whether untrusted build execution may start.
                try:
                    preflight = TarSourceRepository(tmp_path).malicious_preflight(
                        artifact_sha256=sha256.lower(),
                        mode=self._config.static_preflight_v2_mode,
                        audit_recorder=lambda payload: (
                            self._static_preflight_audit.record(
                                agent_id=agent_id,
                                attempt_id=attempt_id,
                                artifact_sha256=sha256.lower(),
                                payload=payload,
                            )
                        ),
                    )
                except StaticPreflightAuditError as error:
                    logger.exception(
                        "static preflight audit failed agent_id=%s attempt_id=%s",
                        agent_id,
                        attempt_id,
                    )
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="static-preflight-audit-failed",
                        summary="static preflight audit journal write failed",
                        detail=f"screener error: {error}",
                    )
                if preflight is not None:
                    logger.warning(
                        "static-source review lead agent_id=%s attempt_id=%s "
                        "categories=%s execution_started=false",
                        agent_id,
                        attempt_id,
                        ",".join(preflight.categories),
                    )
                    resolved_preflight = await self._source_reviewer.resolve_lead(
                        tmp_path,
                        artifact_sha256=sha256.lower(),
                        attempt_id=attempt_id,
                        l1_observation=preflight,
                        progress=(
                            lambda completed, total: report(
                                source_review_progress_stage(completed, total)
                            )
                        ),
                        deadline=deadline,
                        policy_version=policy_version,
                    )
                    if resolved_preflight.ok and resolved_preflight.risk_level == "low":
                        preflight_clearance = resolved_preflight
                    elif (
                        resolved_preflight.adjudication is not None
                        and resolved_preflight.adjudication.get("decision") == "clear"
                    ):
                        # L4 has terminally cleared the static lead, but a full
                        # screen still owes Platform a verified runtime image.
                        # Returning PASS here bypasses build/export and makes
                        # the worker correctly reject the incomplete result.
                        # Carry this exact cleared observation into the normal
                        # post-build policy phase so its signed L4 evidence is
                        # retained on the final verdict.
                        preflight_clearance = resolved_preflight
                    elif resolved_preflight.failure_disposition == "pass_inconclusive":
                        # Continue through cheap mechanical/runtime gates exactly
                        # once and retain this observation for terminal defer.
                        preflight_clearance = resolved_preflight
                    else:
                        decision = self._policy.preexecution_source_decision(
                            resolved_preflight
                        )

                        async def unreachable_challenge(
                            _challenge_id: str,
                            _request: Mapping[str, object],
                            _timeout: float,
                        ) -> ChallengeObservation:
                            raise RuntimeError(
                                "unresolved pre-execution review never starts a "
                                "challenge"
                            )

                        context = PolicyContext(
                            agent_id=agent_id,
                            attempt_id=attempt_id,
                            bench_version=bench_version,
                            miner_hotkey=miner_hotkey,
                            artifact_sha256=sha256.lower(),
                            source_digest=source_digest,
                            source_paths=source_paths,
                            build_elapsed_ms=0,
                            health_elapsed_ms=0,
                            run_challenge=unreachable_challenge,
                            review_source=None,
                        )
                        self._journal.record(context=context, decision=decision)
                        return decision

                if preflight_clearance is None:

                    async def review_with_selected_provider() -> (
                        SourceReviewObservation
                    ):
                        remote_only = self._config.remote_build_mode != "off"
                        if remote_source_review is not None:
                            try:
                                remote = await remote_source_review()
                            except LocalScreeningProviderSelected:
                                return await self._source_reviewer.review(
                                    tmp_path,
                                    artifact_sha256=sha256.lower(),
                                    attempt_id=attempt_id,
                                    progress=report_review_progress,
                                    deadline=deadline,
                                )
                            except Exception:  # noqa: BLE001 - terminal provider failure
                                logger.warning(
                                    "remote source reviewer raised unexpectedly; "
                                    "parking screening attempt",
                                    exc_info=True,
                                )
                            else:
                                if remote is not None:
                                    # Targon/Cloud Run now run L1 then L2/L3 in
                                    # the same rental. A completed observation
                                    # is authoritative so GCE does not re-review.
                                    remote_failed = (
                                        not remote.ok and remote.risk_level is None
                                    )
                                    if not remote_failed or remote_only:
                                        return remote
                        if remote_only and remote_source_review is not None:
                            return SourceReviewObservation(
                                ok=False,
                                risk_level=None,
                                finding_digest=None,
                                categories=(),
                                error_code="targon-source-review-unavailable",
                                failure_disposition="retryable_infra",
                            )
                        return await self._source_reviewer.review(
                            tmp_path,
                            artifact_sha256=sha256.lower(),
                            attempt_id=attempt_id,
                            progress=report_review_progress,
                            deadline=deadline,
                            policy_version=policy_version,
                        )

                    review_factory = review_with_selected_provider
                else:

                    async def cleared_preflight() -> SourceReviewObservation:
                        assert preflight_clearance is not None
                        return preflight_clearance

                    review_factory = cleared_preflight

            if policy_only:
                # A policy-only rescreen has retained build/runtime evidence,
                # so it bypasses the normal post-health point that starts this
                # task.  Start the same deferred source-review task before the
                # policy asks for it; otherwise ``review_source`` asserts and
                # the rescreen is incorrectly reported as infrastructure
                # failure without producing review evidence.
                if review_factory is None:
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="source-review-unavailable",
                        summary="policy-only rescreen could not start source review",
                        detail="screener error: source review was not initialized",
                    )
                review_task = asyncio.create_task(review_factory())

                async def unavailable_challenge(
                    _challenge_id: str,
                    _request: Mapping[str, object],
                    _timeout: float,
                ) -> ChallengeObservation:
                    raise RuntimeError("policy-only rescreen does not start a runtime")

                async def review_source():  # type: ignore[no-untyped-def]
                    nonlocal in_policy_phase
                    in_policy_phase = True
                    assert review_task is not None
                    return await review_task

                context = PolicyContext(
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    bench_version=bench_version,
                    miner_hotkey=miner_hotkey,
                    artifact_sha256=sha256.lower(),
                    source_digest=source_digest,
                    source_paths=source_paths,
                    build_elapsed_ms=0,
                    health_elapsed_ms=0,
                    run_challenge=unavailable_challenge,
                    review_source=review_source,
                )
                report("validating")
                decision = await self._policy.evaluate(context, skip_challenges=True)
                if (
                    decision.outcome == ScreeningOutcome.PASS
                    and preflight_clearance is not None
                    and preflight_clearance.failure_disposition == "pass_inconclusive"
                ):
                    deferred = self._policy.preexecution_source_decision(
                        preflight_clearance
                    )
                    decision = ScreeningDecision(
                        outcome=ScreeningOutcome.PASS_INCONCLUSIVE,
                        detail=deferred.detail,
                        manifest_digest=decision.manifest_digest,
                        evidence=(*deferred.evidence, *decision.evidence),
                        finding=deferred.finding,
                        review_audit=deferred.review_audit,
                    )
                decision = _with_image_binding_advisory(
                    decision, self._image_binding_advisory(tmp_path)
                )
                self._journal.record(context=context, decision=decision)
                return decision

            report("building")
            if (exhausted := self._lease_exhausted(deadline, "build")) is not None:
                return exhausted
            build_timeout = self._config.build_timeout_seconds
            remaining = self._lease_remaining(deadline)
            if remaining is not None:
                build_timeout = min(build_timeout, remaining)
            started = asyncio.get_running_loop().time()
            built = False
            build_detail = ""
            built_image_id: str | None = None
            targon_runtime_ok = False
            local_build_selected = False
            if remote_build is not None:
                try:
                    remote_archive = await remote_build()
                except LocalScreeningProviderSelected:
                    local_build_selected = True
                except RemoteSubmissionBuildRejected:
                    return core_decision(
                        ScreeningOutcome.DETERMINISTIC_REJECT,
                        code="docker-build",
                        summary="artifact Docker image did not build",
                        detail="build failed: DITTO_SUBMISSION_BUILD_FAILED=KANIKO",
                    )
                except Exception:  # noqa: BLE001 - terminal provider failure
                    logger.warning(
                        "remote builder raised unexpectedly; parking screening attempt",
                        exc_info=True,
                    )
            targon_runtime_ok = (
                remote_archive is not None
                and remote_archive.runtime_status == "succeeded"
            )
            if targon_runtime_ok:
                # Targon already booted this exact archive as a Rental and
                # probed /health. Do not import or rebuild it on GCE.
                assert remote_archive is not None
                built = True
                built_image_id = f"sha256:{remote_archive.sha256}"
                build_detail = "targon-runtime-health"
            elif (
                remote_build is not None
                and self._config.remote_build_mode != "off"
                and not local_build_selected
            ):
                return core_decision(
                    ScreeningOutcome.RETRYABLE_INFRA,
                    code="targon-runtime-unavailable",
                    summary="Targon runtime smoke did not admit this archive",
                    detail=(
                        "screener error: remote-only screening requires a "
                        "succeeded Targon runtime health result"
                    ),
                )
            elif remote_archive is not None:
                executor_error = await self._verify_executor()
                if executor_error is not None:
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="executor-isolation-unavailable",
                        summary="screener executor isolation is unavailable",
                        detail=f"screener error: {executor_error}",
                    )
                used_local_docker = True
                try:
                    built, build_detail, built_image_id = await self._load_remote_image(
                        remote_archive.path,
                        build_tag,
                        timeout=min(build_timeout, 120.0),
                    )
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(remote_archive.path)
                if not built:
                    logger.warning(
                        "verified remote archive could not be imported (%s); "
                        "using local Docker",
                        _log_tail(build_detail),
                    )
            if not built:
                executor_error = await self._verify_executor()
                if executor_error is not None:
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="executor-isolation-unavailable",
                        summary="screener executor isolation is unavailable",
                        detail=f"screener error: {executor_error}",
                    )
                used_local_docker = True
                remaining = self._lease_remaining(deadline)
                local_timeout = self._config.build_timeout_seconds
                if remaining is not None:
                    local_timeout = min(local_timeout, remaining)
                built, build_detail, built_image_id = await self._build(
                    tmp_path, build_tag, timeout=local_timeout
                )
            build_elapsed_ms = round(
                (asyncio.get_running_loop().time() - started) * 1000
            )
            if not built:
                retryable = _docker_infrastructure_failure(build_detail)
                return core_decision(
                    ScreeningOutcome.RETRYABLE_INFRA
                    if retryable
                    else ScreeningOutcome.DETERMINISTIC_REJECT,
                    code=(
                        "docker-build-infrastructure" if retryable else "docker-build"
                    ),
                    summary=(
                        "Docker build infrastructure failed"
                        if retryable
                        else "artifact Docker image did not build"
                    ),
                    detail=(
                        f"screener error: Docker build infrastructure: {build_detail}"
                        if retryable
                        else f"build failed: {build_detail}"
                    ),
                )
            if built_image_id is None:
                raise RuntimeError("successful Docker build did not return an image id")

            report("starting")
            exhausted = self._lease_exhausted(deadline, "serve check")
            if exhausted is not None:
                return exhausted
            started = asyncio.get_running_loop().time()
            audit_runtime: _AuditRuntime | None
            if targon_runtime_ok:
                report("health_check")
                serve_result = _StageResult(True, "")
                audit_runtime = _AuditRuntime(
                    harness_base="",
                    gateway_response_token="",
                    oracle_answer="",
                    gateway_state_file="",
                )
            else:
                serve_result, audit_runtime = await self._run_and_probe(
                    built_image_id,
                    container,
                    gateway_container=gateway_container,
                    network=network,
                    gateway_state_dir=gateway_state_dir,
                    progress=report,
                )
            health_elapsed_ms = round(
                (asyncio.get_running_loop().time() - started) * 1000
            )
            if not serve_result.passed:
                outcome = (
                    ScreeningOutcome.RETRYABLE_INFRA
                    if serve_result.retryable
                    else ScreeningOutcome.DETERMINISTIC_REJECT
                )
                prefix = (
                    "screener error" if serve_result.retryable else "serve check failed"
                )
                return core_decision(
                    outcome,
                    code="serve-infrastructure"
                    if serve_result.retryable
                    else "health-contract",
                    summary="screening runtime infrastructure failed"
                    if serve_result.retryable
                    else "container did not satisfy the health contract",
                    detail=f"{prefix}: {serve_result.detail}",
                )
            if audit_runtime is None:
                raise RuntimeError("healthy harness has no isolated audit runtime")

            if review_factory is not None:
                review_task = asyncio.create_task(review_factory())

            async def run_challenge(
                challenge_id: str, request: Mapping[str, object], timeout: float
            ) -> ChallengeObservation:
                return await self._run_private_challenge(
                    challenge_id,
                    request,
                    timeout,
                    harness_base=audit_runtime.harness_base,
                    probe_container=gateway_container,
                    gateway_response_token=audit_runtime.gateway_response_token,
                    oracle_answer=audit_runtime.oracle_answer,
                    gateway_state_file=audit_runtime.gateway_state_file,
                )

            async def review_source():  # type: ignore[no-untyped-def]
                nonlocal in_policy_phase
                in_policy_phase = True
                assert review_task is not None
                return await review_task

            context = PolicyContext(
                agent_id=agent_id,
                attempt_id=attempt_id,
                bench_version=bench_version,
                miner_hotkey=miner_hotkey,
                artifact_sha256=sha256.lower(),
                source_digest=source_digest,
                source_paths=source_paths,
                build_elapsed_ms=build_elapsed_ms,
                health_elapsed_ms=health_elapsed_ms,
                run_challenge=run_challenge,
                # A build-only pass skipped source review, so the policy is
                # given no source-review source and never runs the selector
                # (anti-cheat) phase.
                review_source=None if build_only else review_source,
            )
            report("validating")
            exhausted = self._lease_exhausted(deadline, "policy review")
            if exhausted is not None:
                return exhausted
            decision = await self._policy.evaluate(
                context,
                build_only=build_only,
                deferred_source_review=deferred_source_review,
                skip_challenges=targon_runtime_ok,
            )
            if (
                decision.outcome == ScreeningOutcome.PASS
                and preflight_clearance is not None
                and preflight_clearance.failure_disposition == "pass_inconclusive"
            ):
                deferred = self._policy.preexecution_source_decision(
                    preflight_clearance
                )
                decision = ScreeningDecision(
                    outcome=ScreeningOutcome.PASS_INCONCLUSIVE,
                    detail=deferred.detail,
                    manifest_digest=decision.manifest_digest,
                    evidence=(*deferred.evidence, *decision.evidence),
                    finding=deferred.finding,
                    review_audit=deferred.review_audit,
                )
            # The image-binding advisory can only escalate a PASS to an
            # operator-reviewed QUARANTINE. The mechanical lane collected no
            # source-review evidence, so keep its policy decision as-is and
            # apply the advisory only on a full screen.
            if not build_only:
                decision = _with_image_binding_advisory(
                    decision, self._image_binding_advisory(tmp_path)
                )
            self._journal.record(context=context, decision=decision)
            if (
                decision.outcome
                in {
                    ScreeningOutcome.PASS,
                    ScreeningOutcome.PASS_INCONCLUSIVE,
                }
                and publish_image is not None
            ):
                report("submitting")
                if (
                    exhausted := self._lease_exhausted(deadline, "image export")
                ) is not None:
                    return exhausted
                try:
                    if targon_runtime_ok:
                        assert remote_archive is not None
                        image = await self._export_remote_archive(
                            remote_archive,
                            image_ref=image_ref,
                            deadline=deadline,
                        )
                    else:
                        image = await self._export_image(
                            built_image_id,
                            image_ref=image_ref,
                            deadline=deadline,
                        )
                except _ScreenedImageTooLargeError as error:
                    return core_decision(
                        ScreeningOutcome.DETERMINISTIC_REJECT,
                        code="screened-image-too-large",
                        summary="screened Docker image exceeded the archive size limit",
                        detail=str(error),
                    )
                except _LeaseDeadlineError:
                    return self._lease_exhausted(
                        deadline, "image export"
                    ) or core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="lease-budget-exhausted",
                        summary="screening lease budget exhausted before completion",
                        detail=(
                            "screener error: lease budget exhausted during image export"
                        ),
                    )
                except Exception as error:  # noqa: BLE001 - classify export infra
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="screened-image-export-failed",
                        summary="screened Docker image export failed",
                        detail=f"screener error: image export failed: {error}",
                    )
                try:
                    remaining = self._lease_remaining(deadline)
                    if remaining is None:
                        await publish_image(image)
                    elif remaining <= 0:
                        raise _LeaseDeadlineError
                    else:
                        async with asyncio.timeout(remaining):
                            await publish_image(image)
                except (TimeoutError, _LeaseDeadlineError):
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="lease-budget-exhausted",
                        summary="screening lease budget exhausted before completion",
                        detail=(
                            "screener error: lease budget exhausted during image upload"
                        ),
                    )
                except Exception as error:  # noqa: BLE001 - publish is parked infra
                    return core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="image-upload-failed",
                        summary="screened Docker image upload failed",
                        detail=f"screener error: image upload failed: {error}",
                    )
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(image.path)
            return decision
        except Exception as e:  # noqa: BLE001 - the loop must never die on one agent
            logger.exception("gate error for agent_id=%s", agent_id)
            return core_decision(
                ScreeningOutcome.RETRYABLE_INFRA,
                code="unexpected-infrastructure",
                summary="unexpected screening infrastructure failure",
                detail=f"screener error: {type(e).__name__}: {e}",
            )
        finally:
            if review_task is not None and not review_task.done():
                # The run is over without needing the review (build failure,
                # lease exhaustion, core reject): stop spending LLM tokens.
                review_task.cancel()
            if review_task is not None:
                # Drain so a failed review never surfaces as "exception was
                # never retrieved" noise after the decision is already made.
                with contextlib.suppress(BaseException):
                    await review_task
            teardown_started = loop.time()
            if used_local_docker:
                await self._teardown(
                    container,
                    build_tag,
                    gateway_container=gateway_container,
                    network=network,
                )
            shutil.rmtree(gateway_state_dir, ignore_errors=True)
            if remote_archive is not None:
                with contextlib.suppress(OSError):
                    os.unlink(remote_archive.path)
                if remote_build_consumed is not None:
                    with contextlib.suppress(Exception):
                        await remote_build_consumed(remote_archive.build_id)
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            logger.info(
                "screen timing agent_id=%s total_ms=%d teardown_ms=%d %s",
                agent_id,
                round((loop.time() - screen_started) * 1000),
                round((loop.time() - teardown_started) * 1000),
                _format_stage_timings(stage_history, end=teardown_started),
            )

    # --- lease budget -----------------------------------------------------

    @staticmethod
    def _lease_remaining(deadline: Deadline) -> float | None:
        """Seconds of lease budget left, or ``None`` when no deadline is set."""
        if deadline is None:
            return None
        expires_at = (
            deadline.expires_at if isinstance(deadline, LeaseDeadline) else deadline
        )
        return expires_at - asyncio.get_running_loop().time()

    def _lease_exhausted(
        self, deadline: Deadline, stage: str
    ) -> ScreeningDecision | None:
        """A parked infrastructure decision when the lease cannot fit ``stage``."""
        remaining = self._lease_remaining(deadline)
        if remaining is not None and remaining <= _LEASE_MIN_STAGE_SECONDS:
            logger.warning(
                "screening lease budget exhausted before %s (%.1fs left); "
                "reporting infrastructure failure for manual retry",
                stage,
                remaining,
            )
            return core_decision(
                ScreeningOutcome.RETRYABLE_INFRA,
                code="lease-budget-exhausted",
                summary="screening lease budget exhausted before completion",
                detail=f"screener error: lease budget exhausted before {stage}",
            )
        return None

    # --- stages -----------------------------------------------------------

    @staticmethod
    def _portable_image_archive(
        source_path: str,
        destination_path: str,
        *,
        deadline: Deadline,
    ) -> _PortableImageArchive:
        """Normalize Docker 29 output to the portable pre-OCI save contract.

        Docker 25+ writes an OCI-layout envelope even for a single-platform
        ``docker image save``. Validators that predate that producer change
        intentionally accept the classic Docker save contract instead: one
        config-digest identity, one manifest, and uncompressed ``layer.tar``
        members. Preserve the exact config and filesystem bytes that passed the
        screener while changing only that transport envelope.
        """

        def check_deadline() -> None:
            expires_at = (
                deadline.expires_at if isinstance(deadline, LeaseDeadline) else deadline
            )
            if expires_at is not None and time.monotonic() >= expires_at:
                raise _LeaseDeadlineError(
                    "lease expired during portable image normalization"
                )

        def safe_member_name(name: str) -> str:
            normalized = str(PurePosixPath(name))
            if (
                not name
                or name.startswith("/")
                or normalized != name
                or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
            ):
                raise _ScreenedImageExportError(
                    "Docker image archive contains a non-canonical path"
                )
            return normalized

        def regular_member(
            members: Mapping[str, tarfile.TarInfo], name: str
        ) -> tarfile.TarInfo:
            member = members.get(safe_member_name(name))
            if member is None or not member.isfile():
                raise _ScreenedImageExportError(
                    f"Docker image archive is missing regular member {name!r}"
                )
            return member

        def layer_stream(archive: tarfile.TarFile, member: tarfile.TarInfo) -> BinaryIO:
            raw = archive.extractfile(member)
            if raw is None:
                raise _ScreenedImageExportError(
                    f"Docker image layer {member.name!r} is unreadable"
                )
            prefix = raw.read(2)
            raw.seek(0)
            if prefix == b"\x1f\x8b":
                return cast(BinaryIO, gzip.GzipFile(fileobj=raw, mode="rb"))
            return cast(BinaryIO, raw)

        def copy_info(name: str, size: int) -> tarfile.TarInfo:
            info = tarfile.TarInfo(name)
            info.size = size
            info.mode = 0o600
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            return info

        try:
            with tarfile.open(source_path, mode="r:") as source:
                all_members = source.getmembers()
                if len(all_members) > 4096:
                    raise _ScreenedImageExportError(
                        "Docker image archive contains too many members"
                    )
                members: dict[str, tarfile.TarInfo] = {}
                for member in all_members:
                    name = safe_member_name(member.name)
                    if name in members:
                        raise _ScreenedImageExportError(
                            "Docker image archive contains a duplicate path"
                        )
                    members[name] = member

                manifest_member = regular_member(members, "manifest.json")
                if manifest_member.size <= 0 or manifest_member.size > 1 << 20:
                    raise _ScreenedImageExportError(
                        "Docker image manifest has an invalid size"
                    )
                manifest_file = source.extractfile(manifest_member)
                if manifest_file is None:
                    raise _ScreenedImageExportError(
                        "Docker image manifest is unreadable"
                    )
                try:
                    manifest = json.load(manifest_file)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise _ScreenedImageExportError(
                        "Docker image manifest is invalid JSON"
                    ) from error
                if not isinstance(manifest, list) or len(manifest) != 1:
                    raise _ScreenedImageExportError(
                        "Docker image archive must contain exactly one image"
                    )
                entry = manifest[0]
                if not isinstance(entry, dict):
                    raise _ScreenedImageExportError(
                        "Docker image manifest entry has an invalid shape"
                    )
                config_name = entry.get("Config")
                layer_names = entry.get("Layers")
                repo_tags = entry.get("RepoTags")
                if (
                    not isinstance(config_name, str)
                    or not isinstance(layer_names, list)
                    or not all(isinstance(name, str) for name in layer_names)
                    or len(layer_names) > 256
                    # The remote builders tag their one output with the
                    # attempt-scoped destination.  This normalizer does not
                    # preserve that mutable name: identity remains bound to
                    # the verified config and layer bytes below, and the
                    # portable archive it emits is always untagged.
                    or not (
                        repo_tags is None
                        or (
                            isinstance(repo_tags, list)
                            and len(repo_tags) <= 1
                            and all(
                                isinstance(tag, str) and 0 < len(tag) <= 512
                                for tag in repo_tags
                            )
                        )
                    )
                ):
                    raise _ScreenedImageExportError(
                        "Docker image manifest entry has an invalid shape"
                    )

                config_member = regular_member(members, config_name)
                if config_member.size <= 0 or config_member.size > 4 << 20:
                    raise _ScreenedImageExportError(
                        "Docker image config has an invalid size"
                    )
                config_file = source.extractfile(config_member)
                if config_file is None:
                    raise _ScreenedImageExportError("Docker image config is unreadable")
                config_bytes = config_file.read(config_member.size + 1)
                if len(config_bytes) != config_member.size:
                    raise _ScreenedImageExportError("Docker image config is truncated")
                try:
                    config = json.loads(config_bytes)
                    diff_ids = config["rootfs"]["diff_ids"]
                except (json.JSONDecodeError, KeyError, TypeError) as error:
                    raise _ScreenedImageExportError(
                        "Docker image config has an invalid rootfs"
                    ) from error
                if (
                    not isinstance(diff_ids, list)
                    or len(diff_ids) != len(layer_names)
                    or not all(
                        isinstance(digest, str)
                        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                        for digest in diff_ids
                    )
                ):
                    raise _ScreenedImageExportError(
                        "Docker image layer identities do not match the manifest"
                    )

                portable_layers: list[tuple[str, tarfile.TarInfo, int]] = []
                chain_id: str | None = None
                projected_size = 10240
                for layer_name, diff_id in zip(layer_names, diff_ids, strict=True):
                    check_deadline()
                    member = regular_member(members, layer_name)
                    digest = hashlib.sha256()
                    expanded_size = 0
                    stream = layer_stream(source, member)
                    try:
                        while chunk := stream.read(_IMAGE_HASH_CHUNK_BYTES):
                            check_deadline()
                            digest.update(chunk)
                            expanded_size += len(chunk)
                            if expanded_size > _MAX_SCREENED_IMAGE_BYTES:
                                raise _ScreenedImageTooLargeError(
                                    "screened image archive expands beyond the size cap"
                                )
                    except (gzip.BadGzipFile, EOFError, OSError) as error:
                        raise _ScreenedImageExportError(
                            f"Docker image layer {layer_name!r} is not readable"
                        ) from error
                    finally:
                        stream.close()
                    if "sha256:" + digest.hexdigest() != diff_id:
                        raise _ScreenedImageExportError(
                            "Docker image layer bytes do not match the config digest"
                        )
                    if chain_id is None:
                        chain_id = diff_id
                    else:
                        chain_id = (
                            "sha256:"
                            + hashlib.sha256(
                                f"{chain_id} {diff_id}".encode()
                            ).hexdigest()
                        )
                    portable_name = chain_id.removeprefix("sha256:") + "/layer.tar"
                    portable_layers.append((portable_name, member, expanded_size))
                    projected_size += 512 + ((expanded_size + 511) // 512) * 512

                config_hex = hashlib.sha256(config_bytes).hexdigest()
                portable_manifest = json.dumps(
                    [
                        {
                            "Config": f"{config_hex}.json",
                            "RepoTags": None,
                            "Layers": [name for name, _, _ in portable_layers],
                        }
                    ],
                    separators=(",", ":"),
                ).encode()
                projected_size += 1024
                projected_size += 512 + ((len(config_bytes) + 511) // 512) * 512
                projected_size += 512 + ((len(portable_manifest) + 511) // 512) * 512
                if projected_size > _MAX_SCREENED_IMAGE_BYTES:
                    raise _ScreenedImageTooLargeError(
                        "portable screened image archive exceeds the size cap"
                    )

                with tarfile.open(destination_path, mode="w:") as destination:
                    destination.addfile(
                        copy_info("manifest.json", len(portable_manifest)),
                        io.BytesIO(portable_manifest),
                    )
                    destination.addfile(
                        copy_info(f"{config_hex}.json", len(config_bytes)),
                        io.BytesIO(config_bytes),
                    )
                    for portable_name, member, expanded_size in portable_layers:
                        check_deadline()
                        stream = layer_stream(source, member)
                        try:
                            destination.addfile(
                                copy_info(portable_name, expanded_size), stream
                            )
                        except (gzip.BadGzipFile, EOFError, OSError) as error:
                            raise _ScreenedImageExportError(
                                f"Docker image layer {member.name!r} is not readable"
                            ) from error
                        finally:
                            stream.close()
        except tarfile.TarError as error:
            raise _ScreenedImageExportError(
                "Docker image save output is not a readable tar archive"
            ) from error

        return _PortableImageArchive(
            path=destination_path,
            image_id=f"sha256:{config_hex}",
        )

    async def _export_remote_archive(
        self,
        archive: RemoteImageArchive,
        *,
        image_ref: str,
        deadline: Deadline,
    ) -> BuiltImageArtifact:
        """Publish the Platform-verified Kaniko tar without a local docker save."""
        if archive.size_bytes > _MAX_SCREENED_IMAGE_BYTES:
            raise _ScreenedImageTooLargeError(
                f"screened image exceeds {_MAX_SCREENED_IMAGE_BYTES} byte cap"
            )
        fd, created_path = tempfile.mkstemp(
            prefix="ditto-portable-image-", suffix=".tar"
        )
        os.close(fd)
        portable_path: str | None = created_path
        try:
            portable = await asyncio.to_thread(
                self._portable_image_archive,
                archive.path,
                created_path,
                deadline=deadline,
            )
            portable_path = None
            size_bytes = os.path.getsize(portable.path)
            if size_bytes > _MAX_SCREENED_IMAGE_BYTES:
                raise _ScreenedImageTooLargeError(
                    "screened image archive exceeds "
                    f"{_MAX_SCREENED_IMAGE_BYTES} byte cap"
                )
            sha256 = await self._hash_image_archive(portable.path, deadline=deadline)
            return BuiltImageArtifact(
                path=portable.path,
                sha256=sha256,
                size_bytes=size_bytes,
                image_id=portable.image_id,
                image_ref=image_ref,
            )
        except BaseException:
            if portable_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(portable_path)
            raise

    async def _export_image(
        self,
        image_id: str,
        *,
        image_ref: str,
        deadline: Deadline,
    ) -> BuiltImageArtifact:
        """Export the exact screened image before teardown and hash its bytes."""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise _ScreenedImageExportError("Docker returned an invalid image id")
        path: str | None = None
        portable_path: str | None = None
        try:
            inspect_timeout = self._lease_timeout(deadline, 30.0, "image inspection")
            code, raw_size = await self._run(
                ["image", "inspect", "--format", "{{.Size}}", image_id],
                timeout=inspect_timeout,
            )
            if code != 0:
                raise _ScreenedImageExportError(
                    f"docker image inspect failed: {_log_tail(raw_size)}"
                )
            try:
                image_size = int(raw_size.strip())
            except ValueError as error:
                raise _ScreenedImageExportError(
                    "docker returned an invalid image size"
                ) from error
            if image_size > _MAX_SCREENED_IMAGE_BYTES:
                raise _ScreenedImageTooLargeError(
                    f"screened image exceeds {_MAX_SCREENED_IMAGE_BYTES} byte cap"
                )

            fd, path = tempfile.mkstemp(prefix="ditto-screened-image-", suffix=".tar")
            os.close(fd)
            free_bytes = shutil.disk_usage(Path(path).parent).free
            required_bytes = image_size * 2 + _IMAGE_EXPORT_DISK_RESERVE_BYTES
            if free_bytes < required_bytes:
                raise _ScreenedImageExportError(
                    "insufficient temporary disk for screened image export "
                    f"(need {required_bytes} bytes, have {free_bytes})"
                )
            export_timeout = self._lease_timeout(deadline, 600.0, "image export")
            code, output = await self._run(
                ["image", "save", "--output", path, image_id],
                timeout=export_timeout,
            )
            if code != 0:
                raise _ScreenedImageExportError(
                    f"docker image export failed: {_log_tail(output)}"
                )
            fd, portable_path = tempfile.mkstemp(
                prefix="ditto-portable-image-", suffix=".tar"
            )
            os.close(fd)
            portable = await asyncio.to_thread(
                self._portable_image_archive,
                path,
                portable_path,
                deadline=deadline,
            )
            os.unlink(path)
            path = portable.path
            portable_path = None
            size_bytes = os.path.getsize(path)
            if size_bytes > _MAX_SCREENED_IMAGE_BYTES:
                raise _ScreenedImageTooLargeError(
                    "screened image archive exceeds "
                    f"{_MAX_SCREENED_IMAGE_BYTES} byte cap"
                )
            sha256 = await self._hash_image_archive(path, deadline=deadline)
            return BuiltImageArtifact(
                path=path,
                sha256=sha256,
                size_bytes=size_bytes,
                image_id=portable.image_id,
                image_ref=image_ref,
            )
        except BaseException:
            if path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            if portable_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(portable_path)
            raise

    def _lease_timeout(self, deadline: Deadline, cap: float, stage: str) -> float:
        """Clamp one operation to remaining lease time without post-expiry grace."""
        remaining = self._lease_remaining(deadline)
        if remaining is None:
            return cap
        if remaining <= 0:
            raise _LeaseDeadlineError(f"lease expired before {stage}")
        return min(cap, remaining)

    async def _hash_image_archive(self, path: str, *, deadline: Deadline) -> str:
        """Hash the archive incrementally while enforcing the lease deadline."""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                timeout = self._lease_timeout(deadline, 30.0, "image hashing")
                try:
                    chunk = await asyncio.wait_for(
                        asyncio.to_thread(handle.read, _IMAGE_HASH_CHUNK_BYTES),
                        timeout=timeout,
                    )
                except TimeoutError as error:
                    raise _LeaseDeadlineError(
                        "lease expired during image hashing"
                    ) from error
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    async def _download_verified(
        self, url: str, expected_sha256: str
    ) -> tuple[str | None, str]:
        """Stream the tarball to a temp file, size-bounded + sha256-checked.

        Returns ``(path, "")`` on success or ``(None, reason)`` on a cap breach,
        digest mismatch, or transport error.
        """
        cap = self._config.max_tarball_bytes
        hasher = hashlib.sha256()
        total = 0
        fd, path = tempfile.mkstemp(prefix="ditto-screen-", suffix=".tar.gz")
        keep_path = False
        try:
            with os.fdopen(fd, "wb") as fh:
                async with self._client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return None, f"artifact download HTTP {resp.status_code}"
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > cap:
                            return None, f"tarball exceeds {cap} byte cap"
                        hasher.update(chunk)
                        fh.write(chunk)
            digest = hasher.hexdigest()
            if digest != expected_sha256.lower():
                return None, f"sha256 mismatch (got {digest[:12]}…)"
            keep_path = True
            return path, ""
        except httpx.HTTPError as e:
            return None, f"artifact download failed: {e}"
        finally:
            if not keep_path:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def _image_binding_advisory(self, tar_path: str) -> str | None:
        """Run the advisory image/crate binding heuristic over the Dockerfile."""
        try:
            with tarfile.open(tar_path, mode="r:gz") as tar:
                for name in ("Dockerfile", "./Dockerfile"):
                    try:
                        member = tar.getmember(name)
                    except KeyError:
                        continue
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        return None
                    text = extracted.read(1024 * 1024).decode("utf-8", "replace")
                    return image_binding_advisory(text)
        except (tarfile.TarError, OSError):
            return None
        return None

    def _contract_error(self, tar_path: str) -> str | None:
        """Validate the archive and container contract without extracting it."""
        try:
            with tarfile.open(tar_path, mode="r:gz") as tar:
                members: dict[str, tarfile.TarInfo] = {}
                unpacked = 0
                for member_count, member in enumerate(tar, start=1):
                    if member_count > _MAX_ARCHIVE_MEMBERS:
                        return _contract_diagnostic(
                            "SCR-ARCHIVE-005",
                            "archive contains too many members",
                            "remove generated directories and package only the harness",
                        )
                    name = member.name.removeprefix("./")
                    if not name and member.isdir():
                        continue
                    path = PurePosixPath(name)
                    if (
                        not name
                        or name.startswith("/")
                        or "\\" in name
                        or (path.parts and path.parts[0].endswith(":"))
                        or ".." in path.parts
                    ):
                        return _contract_diagnostic(
                            "SCR-ARCHIVE-001",
                            "archive contains an unsafe path",
                            "remove absolute paths, parent traversals, backslashes, "
                            "and drive-prefixed entries",
                        )
                    if str(path) != name:
                        return _contract_diagnostic(
                            "SCR-ARCHIVE-001",
                            "archive contains a non-canonical path",
                            "remove redundant path separators and dot components",
                        )
                    if name in members:
                        return _contract_diagnostic(
                            "SCR-ARCHIVE-002",
                            "archive contains a duplicate path",
                            "package each path exactly once",
                        )
                    if not (member.isfile() or member.isdir()):
                        return _contract_diagnostic(
                            "SCR-ARCHIVE-003",
                            "archive contains a link or special file",
                            "package only regular files and directories",
                        )
                    unpacked += member.size
                    if unpacked > _MAX_UNPACKED_BYTES:
                        return _contract_diagnostic(
                            "SCR-ARCHIVE-004",
                            "archive expands beyond the safety limit",
                            "remove generated assets and build output before packaging",
                        )
                    members[name] = member

                if "Dockerfile" not in members or not members["Dockerfile"].isfile():
                    return _contract_diagnostic(
                        "SCR-CONTRACT-001",
                        "Dockerfile is missing from the archive root",
                        "package the harness contents so Dockerfile is at the "
                        "top level",
                    )
                dockerfile_file = tar.extractfile(members["Dockerfile"])
                if dockerfile_file is None:
                    return _contract_diagnostic(
                        "SCR-CONTRACT-002",
                        "Dockerfile could not be read",
                        "recreate the archive from readable regular files",
                    )
                try:
                    dockerfile_text = dockerfile_file.read().decode("utf-8")
                except UnicodeDecodeError:
                    return _contract_diagnostic(
                        "SCR-CONTRACT-003",
                        "Dockerfile is not valid UTF-8 text",
                        "commit a readable UTF-8 Dockerfile that builds the harness",
                    )
                for instruction, remainder in _dockerfile_instructions(dockerfile_text):
                    lowered = remainder.casefold()
                    if instruction == "RUN" and (
                        "--security=insecure" in lowered or "--network=host" in lowered
                    ):
                        return _contract_diagnostic(
                            "SCR-CONTRACT-004",
                            "Dockerfile requests an insecure build entitlement",
                            "remove RUN --security=insecure and RUN --network=host",
                        )
                # Image/source binding is a text heuristic, so it is applied as
                # advisory quarantine evidence after policy evaluation (see
                # _image_binding_advisory), never as a contract rejection.
                return None
        except (tarfile.TarError, OSError) as e:
            logger.warning("could not read tar %s: %s", tar_path, e)
            return _contract_diagnostic(
                "SCR-ARCHIVE-006",
                "archive is not a readable gzip-compressed tar",
                "recreate it as a .tar.gz archive and retry",
            )

    def _source_metadata(self, tar_path: str) -> tuple[str, tuple[str, ...]]:
        """Return a canonical content digest and bounded normalized path list."""
        digest = hashlib.sha256()
        paths: list[str] = []
        with tarfile.open(tar_path, mode="r:gz") as tar:
            members = sorted(
                (member for member in tar.getmembers() if member.isfile()),
                key=lambda member: member.name.removeprefix("./"),
            )
            for member in members:
                name = member.name.removeprefix("./")
                digest.update(name.encode("utf-8"))
                digest.update(b"\0")
                digest.update(str(member.size).encode("ascii"))
                digest.update(b"\0")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ValueError(f"archive member {name!r} is unreadable")
                while chunk := extracted.read(64 * 1024):
                    digest.update(chunk)
                digest.update(b"\0")
                if len(paths) < 256:
                    paths.append(name)
        return digest.hexdigest(), tuple(paths)

    async def _verify_executor(self) -> str | None:
        """Fail closed when deployment policy requires a rootless daemon."""
        if not self._config.require_rootless_docker or self._executor_verified:
            return None
        code, output = await self._run(
            ["info", "--format", "{{json .SecurityOptions}}"], timeout=15.0
        )
        if code != 0:
            return f"could not inspect Docker security options: {_log_tail(output)}"
        try:
            options = json.loads(output)
        except json.JSONDecodeError:
            return "Docker returned invalid security options"
        if not isinstance(options, list) or not any(
            isinstance(option, str) and "rootless" in option.casefold()
            for option in options
        ):
            return "Docker endpoint is not rootless"
        self._executor_verified = True
        return None

    async def _build(
        self,
        tar_path: str,
        tag: str,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, str, str | None]:
        """``docker build`` from the tarball-on-stdin; returns (ok, log_tail).

        ``timeout`` overrides the configured build cap so the worker can clamp a
        build to the remaining lease budget; it defaults to the full cap.
        """
        fd, iid_path = tempfile.mkstemp(prefix="ditto-screen-iid-")
        os.close(fd)
        os.unlink(iid_path)
        args = [
            "build",
            # The screener immediately inspects, runs, and exports this image
            # through the local daemon. Buildx can otherwise leave a successful
            # result only in its cache while still writing an iidfile, making
            # the post-build image inspect fail after an expensive compile.
            "--load",
            "--iidfile",
            iid_path,
            "--network",
            # BuildKit accepts only default, none, and host here. The default
            # sandbox keeps dependency downloads working; the dedicated
            # rootless daemon and host egress guard provide the trust boundary.
            "default",
            "--pull=false",
            "--provenance=false",
            "--sbom=false",
            "--memory",
            self._config.image_build_memory,
            "--memory-swap",
            self._config.image_build_memory,
            "--cpu-period",
            "100000",
            "--cpu-quota",
            "200000",
            "--shm-size",
            "64m",
            "--ulimit",
            "nofile=1024:1024",
            "-t",
            tag,
            "-f",
            "Dockerfile",
        ]
        env = dict(os.environ)
        env["DOCKER_BUILDKIT"] = "1"
        # No build-time credential is mounted. The build context (a
        # submission-controlled Dockerfile) runs with network access, so any
        # secret exposed here — a BuildKit secret, or the GCE metadata SA token
        # reachable at 169.254.169.254 — is exfiltratable by a hostile RUN step.
        # The only former consumer, the private ``ditto-harness`` dep, is now
        # public and fetches over anonymous HTTPS, so the ``gh_token`` mount was
        # removed. Metadata access is additionally blocked at the host firewall
        # (see the IMDS guard in scripts/bootstrap-screener.sh) as defense in
        # depth for the shared runtime SA.
        args.append("-")  # build context comes from stdin
        if timeout is None:
            timeout = self._config.build_timeout_seconds
        try:
            with _normalized_build_context(tar_path) as stdin_f:
                code, out = await self._run(
                    args, stdin=stdin_f, timeout=timeout, env=env
                )
            if code == 0:
                try:
                    build_result_id = Path(iid_path).read_text().strip()
                except OSError as error:
                    return False, f"Docker did not write iidfile: {error}", None
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", build_result_id):
                    return False, "Docker wrote an invalid image id", None
                # Buildx's iidfile identifies the immutable build result, but
                # that digest is not guaranteed to be an image ID accepted by
                # the local daemon even when ``--load`` succeeds. Resolve the
                # daemon-owned ID from this attempt's unique tag, then pin all
                # runtime and export operations to that immutable local ID.
                inspect_code, image_id = await self._run(
                    ["image", "inspect", "--format", "{{.Id}}", tag],
                    timeout=min(timeout, 30.0),
                )
                image_id = image_id.strip()
                if inspect_code != 0:
                    return (
                        False,
                        f"docker image inspect failed: {_log_tail(image_id)}",
                        None,
                    )
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                    return False, "docker image inspect returned invalid image id", None
                inspect_code, volumes = await self._run(
                    [
                        "image",
                        "inspect",
                        "--format",
                        "{{if .Config.Volumes}}declared{{end}}",
                        image_id,
                    ],
                    timeout=min(timeout, 30.0),
                )
                if inspect_code != 0:
                    return (
                        False,
                        f"docker image inspect failed: {_log_tail(volumes)}",
                        None,
                    )
                if volumes.strip() == "declared":
                    return (
                        False,
                        "image declares writable volumes; harness images must use "
                        "only the validator-owned bounded /tmp tmpfs",
                        None,
                    )
                if volumes.strip():
                    return False, "docker image inspect returned invalid output", None
                return True, "", image_id
        finally:
            with contextlib.suppress(OSError):
                os.unlink(iid_path)
        if code < 0:
            signal_name = signal.Signals(-code).name
            return (
                False,
                (f"docker command exited with signal {signal_name}: {_log_tail(out)}"),
                None,
            )
        if code in {137, 143}:
            return (
                False,
                (f"docker command exited after signal ({code}): {_log_tail(out)}"),
                None,
            )
        return False, _log_tail(out), None

    async def _load_remote_image(
        self,
        path: str,
        tag: str,
        *,
        timeout: float,
    ) -> tuple[bool, str, str | None]:
        """Import a Platform-verified Kaniko archive into the local daemon."""
        code, output = await self._run(
            ["image", "load", "--input", path], timeout=timeout
        )
        if code != 0:
            return False, f"docker image load failed: {_log_tail(output)}", None
        inspect_code, image_id = await self._run(
            ["image", "inspect", "--format", "{{.Id}}", tag],
            timeout=min(timeout, 30.0),
        )
        image_id = image_id.strip()
        if inspect_code != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            return False, "docker image inspect failed after remote load", None
        inspect_code, volumes = await self._run(
            [
                "image",
                "inspect",
                "--format",
                "{{if .Config.Volumes}}declared{{end}}",
                image_id,
            ],
            timeout=min(timeout, 30.0),
        )
        if inspect_code != 0:
            return False, "docker image inspect failed after remote load", None
        if volumes.strip() == "declared":
            return (
                False,
                "image declares writable volumes; harness images must use only "
                "the validator-owned bounded /tmp tmpfs",
                None,
            )
        if volumes.strip():
            return False, "docker image inspect returned invalid output", None
        return True, "", image_id

    async def _run_and_probe(
        self,
        tag: str,
        container: str,
        *,
        gateway_container: str,
        network: str,
        gateway_state_dir: str,
        progress: Callable[[ScreenerProgressStage], None] | None = None,
    ) -> tuple[_StageResult, _AuditRuntime | None]:
        """Run the image and await health against the isolated fake gateway."""
        port = self._config.container_port
        # High-entropy, opaque tokens with no ``ditto``/``fake``/``screening``
        # marker: the first is the per-container nonce the gateway returns, the
        # second is the answer it returns only once the nonce is fed back on a
        # second round-trip (the gateway-encoded correctness oracle).
        response_text = secrets.token_hex(16)
        oracle_answer = secrets.token_hex(16)
        started, detail = await self._start_fake_gateway(
            gateway_container=gateway_container,
            network=network,
            response_text=response_text,
            oracle_answer=oracle_answer,
            state_dir=gateway_state_dir,
        )
        if not started:
            return _StageResult(False, detail, retryable=True), None

        chat_gateway = f"http://{_GATEWAY_ALIAS}:{_CHAT_GATEWAY_PORT}"
        embed_gateway = f"http://{_GATEWAY_ALIAS}:{_EMBED_GATEWAY_PORT}"
        run_args = [
            "run",
            "-d",
            "--init",
            "--name",
            container,
            "--user",
            _VALIDATOR_SANDBOX_USER,
            "--read-only",
            "--ipc",
            "none",
            "--tmpfs",
            _VALIDATOR_SANDBOX_TMPFS,
            "--network",
            network,
            "--network-alias",
            _HARNESS_ALIAS,
            "--memory",
            _VALIDATOR_SANDBOX_MEMORY,
            "--cpus",
            _VALIDATOR_SANDBOX_CPUS,
            "--pids-limit",
            _VALIDATOR_SANDBOX_PIDS,
            "--ulimit",
            "nofile=1024:1024",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=8m",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
        ]
        for key, value in self._config.smoke_env:
            if key == "DITTOBENCH_DB":
                continue
            run_args += ["-e", f"{key}={value}"]
        # Mirror the production scorer's locked provider contract. These are
        # appended last so an operator's legacy smoke env cannot bypass the
        # fake gateway.
        gateway_env = {
            "DITTOBENCH_PROVIDER": "chutes",
            "DITTOBENCH_MODEL": LOCKED_HARNESS_MODEL,
            "CHUTES_BASE_URL": f"{chat_gateway}/v1",
            "CHUTES_API_KEY": "relay",
            "OPENAI_BASE_URL": f"{chat_gateway}/v1",
            "OPENAI_API_KEY": "relay",
            "OLLAMA_BASE_URL": embed_gateway,
            # The validator root filesystem is read-only. Its bounded /tmp
            # tmpfs is the canonical writable location for the harness DB.
            "DITTOBENCH_DB": _VALIDATOR_SANDBOX_DB,
        }
        for key, value in gateway_env.items():
            run_args += ["-e", f"{key}={value}"]
        run_args.append(tag)
        code, out = await self._run(run_args, timeout=self._config.run_timeout_seconds)
        if code != 0:
            return (
                _StageResult(
                    False,
                    f"container did not start: {_log_tail(out)}",
                    retryable=_docker_infrastructure_failure(out),
                ),
                None,
            )

        harness_base = f"http://{_HARNESS_ALIAS}:{port}"
        if progress is not None:
            progress("health_check")
        healthy, detail = await self._wait_healthy(
            harness_base,
            probe_container=gateway_container,
            harness_container=container,
        )
        if not healthy:
            return (
                _StageResult(
                    False,
                    await self._with_container_logs(
                        detail,
                        harness_container=container,
                        gateway_container=gateway_container,
                    ),
                ),
                None,
            )
        # Production v6 intentionally stops here. No synthetic POST /run is
        # issued unless a private policy selector explicitly chooses an audit.
        return (
            _StageResult(True, ""),
            _AuditRuntime(
                harness_base=harness_base,
                gateway_response_token=response_text,
                oracle_answer=oracle_answer,
                gateway_state_file=str(Path(gateway_state_dir) / "model-called"),
            ),
        )

    async def _start_fake_gateway(
        self,
        *,
        gateway_container: str,
        network: str,
        response_text: str,
        oracle_answer: str,
        state_dir: str,
    ) -> tuple[bool, str]:
        """Start the fake gateway beside the harness on an internal network."""
        code, out = await self._run(
            ["network", "create", "--internal", network], timeout=30.0
        )
        if code != 0:
            return False, f"could not create isolated network: {_log_tail(out)}"

        script = str(Path(state_dir) / "fake_gateway.py")
        code, out = await self._run(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                gateway_container,
                "--user",
                # Root only inside the rootless daemon's user namespace; on the
                # host this maps to the empty ditto-builder identity.
                "0:0",
                "--network",
                network,
                "--network-alias",
                _GATEWAY_ALIAS,
                "--read-only",
                "--ipc",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=2m",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
                "--memory",
                "64m",
                "--pids-limit",
                "32",
                "-e",
                f"DITTO_FAKE_GATEWAY_RESPONSE={response_text}",
                "-e",
                f"DITTO_FAKE_GATEWAY_ORACLE_ANSWER={oracle_answer}",
                "-e",
                "DITTO_FAKE_GATEWAY_STATE_FILE=/state/model-called",
                "-v",
                f"{script}:/app/fake_gateway.py:ro",
                "-v",
                f"{state_dir}:/state",
                _CANARY_IMAGE,
                "python",
                "/app/fake_gateway.py",
            ],
            timeout=self._config.run_timeout_seconds,
        )
        if code != 0:
            return False, f"fake gateway did not start: {_log_tail(out)}"

        probe = """\
import socket
for port in (11434, 11435):
    socket.create_connection(('127.0.0.1', port), 2).close()
"""
        for _ in range(20):
            code, _ = await self._run(
                ["exec", gateway_container, "python", "-c", probe], timeout=5.0
            )
            if code == 0:
                return True, ""
            await asyncio.sleep(0.1)
        return False, "fake gateway did not become ready"

    async def _wait_healthy(
        self,
        harness_base: str,
        *,
        probe_container: str | None = None,
        harness_container: str | None = None,
    ) -> tuple[bool, str]:
        """Poll the submitted container's health endpoint until the deadline."""
        url = f"{harness_base}{self._config.health_path}"
        deadline = self._config.run_timeout_seconds
        waited = 0.0
        last = "no response"
        while waited < deadline:
            if probe_container is not None:
                code, out = await self._request_from_sidecar(
                    probe_container, url, timeout=5.0
                )
                if code == 0:
                    return True, ""
                last = _log_tail(out) or "unreachable"
                if harness_container is not None:
                    inspect_code, lifecycle = await self._run(
                        [
                            "container",
                            "inspect",
                            "--format",
                            "{{.State.Status}}",
                            harness_container,
                        ],
                        timeout=5.0,
                    )
                    lifecycle = lifecycle.strip().lower()
                    if inspect_code == 0 and lifecycle in {"dead", "exited"}:
                        return (
                            False,
                            "harness exited before its health endpoint became ready "
                            f"({last})",
                        )
            else:
                try:
                    resp = await self._client.get(url, timeout=5.0)
                    if 200 <= resp.status_code < 300:
                        return True, ""
                    last = f"HTTP {resp.status_code}"
                except httpx.HTTPError as e:
                    last = type(e).__name__
            await asyncio.sleep(_PROBE_INTERVAL_SECONDS)
            waited += _PROBE_INTERVAL_SECONDS
        return False, f"/health never healthy within {deadline:g}s ({last})"

    async def _run_private_challenge(
        self,
        challenge_id: str,
        request: Mapping[str, object],
        timeout: float,
        *,
        harness_base: str,
        probe_container: str,
        gateway_response_token: str,
        gateway_state_file: str,
        oracle_answer: str | None = None,
    ) -> ChallengeObservation:
        """Run one selected private challenge and retain only bounded evidence.

        Timing and gateway-call counts are objective, reproducible facts about
        the isolated round-trip. ``oracle_answer_correct`` is likewise objective:
        the harness can only surface ``oracle_answer`` by feeding the gateway
        nonce back through a second turn, which a static table cannot do.
        """
        # A tool-shaped challenge (non-empty `tools`) needs a reachable
        # `tool_endpoint` so the harness's agent loop can execute the tool call
        # the model returns and proceed to the second model turn. Filled here
        # (not in the policy module) because only the gate knows the network
        # topology.
        payload = _with_tool_endpoint(request)
        calls_before = _gateway_call_count(gateway_state_file)
        started = asyncio.get_running_loop().time()
        code, out = await self._request_from_sidecar(
            probe_container,
            f"{harness_base}/run",
            payload=payload,
            timeout=min(timeout, self._config.run_timeout_seconds),
        )
        elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        gateway_calls = max(0, _gateway_call_count(gateway_state_file) - calls_before)
        if code != 0:
            # The probe output carries the concrete failure ("HTTP 422: ...",
            # a timeout traceback, ...). Log it bounded: a silent discard here
            # previously hid a request-contract break behind an opaque
            # "challenge-http-failure" for every screening.
            logger.warning(
                "private challenge %s HTTP failure: exit=%d detail=%.400s",
                challenge_id,
                code,
                out,
            )
            return ChallengeObservation(
                challenge_id=challenge_id,
                ok=False,
                response_digest=None,
                elapsed_ms=elapsed_ms,
                error_code="challenge-http-failure",
                gateway_calls=gateway_calls,
            )
        body = out.encode()
        response_digest = hashlib.sha256(body).hexdigest()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ChallengeObservation(
                challenge_id=challenge_id,
                ok=False,
                response_digest=response_digest,
                elapsed_ms=elapsed_ms,
                error_code="challenge-invalid-json",
                gateway_calls=gateway_calls,
            )
        if not isinstance(payload, dict):
            return ChallengeObservation(
                challenge_id=challenge_id,
                ok=False,
                response_digest=response_digest,
                elapsed_ms=elapsed_ms,
                error_code="challenge-invalid-shape",
                gateway_calls=gateway_calls,
            )
        return ChallengeObservation(
            challenge_id=challenge_id,
            ok=True,
            response_digest=response_digest,
            elapsed_ms=elapsed_ms,
            json_keys=tuple(sorted(str(key) for key in payload)[:64]),
            gateway_calls=gateway_calls,
            # Either token proves binding to THIS container's ephemeral
            # gateway: with the tool-call first turn the nonce is consumed
            # inside the transcript and the surfaced final text is the oracle
            # answer, so both must count.
            gateway_token_observed=(
                _contains_string(payload, gateway_response_token)
                or (
                    oracle_answer is not None
                    and _contains_string(payload, oracle_answer)
                )
            ),
            oracle_answer_correct=(
                oracle_answer is not None and _contains_string(payload, oracle_answer)
            ),
        )

    async def _request_from_sidecar(
        self,
        container: str,
        url: str,
        *,
        payload: Mapping[str, object] | None = None,
        timeout: float,
    ) -> tuple[int, str]:
        """Make an HTTP request from inside the isolated Docker network."""
        encoded = ""
        method = "GET"
        if payload is not None:
            encoded = base64.b64encode(json.dumps(payload).encode()).decode()
            method = "POST"
        script = f"""\
import base64
import sys
import urllib.error
import urllib.request

url, method, data, timeout_raw = sys.argv[1:5]
body = base64.b64decode(data) if data else None
request = urllib.request.Request(
    url, data=body, method=method, headers={{"Content-Type": "application/json"}}
)
try:
    response = urllib.request.urlopen(request, timeout=float(timeout_raw))
except urllib.error.HTTPError as error:
    response = error
output = response.read({_MAX_CANARY_RESPONSE_BYTES + 1})
if len(output) > {_MAX_CANARY_RESPONSE_BYTES}:
    sys.stdout.write("response exceeded safety cap")
    raise SystemExit(23)
if not 200 <= response.status < 300:
    sys.stdout.buffer.write(f"HTTP {{response.status}}: ".encode() + output)
    raise SystemExit(22)
sys.stdout.buffer.write(output)
"""
        return await self._run(
            [
                "exec",
                container,
                "python",
                "-c",
                script,
                url,
                method,
                encoded,
                str(timeout),
            ],
            timeout=timeout,
        )

    async def _with_container_logs(
        self,
        detail: str,
        *,
        harness_container: str,
        gateway_container: str,
    ) -> str:
        """Attach bounded Docker logs before teardown removes the containers."""
        sections: list[str] = []
        for label, container in (
            ("harness", harness_container),
            ("fake-gateway", gateway_container),
        ):
            _code, output = await self._run(["logs", container], timeout=15.0)
            if output.strip():
                sections.append(f"{label} logs:\n{_log_tail(output)}")
        if not sections:
            return detail
        diagnostics = _log_tail("\n".join(sections))
        logger.warning("screener container diagnostics: %s", diagnostics)
        return _detail_tail(f"{detail}\n{diagnostics}")

    async def _teardown(
        self,
        container: str,
        tag: str,
        *,
        gateway_container: str,
        network: str,
    ) -> None:
        """Best-effort removal of the container + image; never raises."""
        try:
            # Both containers can be removed concurrently; the network can
            # only go once its endpoints are gone, and the image untag is
            # independent of the network.
            await asyncio.gather(
                self._run(["rm", "-f", container], timeout=30.0),
                self._run(["rm", "-f", gateway_container], timeout=30.0),
            )
            await asyncio.gather(
                self._run(["network", "rm", network], timeout=30.0),
                self._run(["rmi", "-f", tag], timeout=30.0),
            )
        except Exception:  # noqa: BLE001 - teardown must never mask a result
            logger.warning("teardown issue for %s / %s", container, tag, exc_info=True)

    # --- subprocess -------------------------------------------------------

    async def _run(
        self,
        args: list[str],
        *,
        stdin: io.BufferedReader | None = None,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run ``docker <args>`` with a hard timeout; return (returncode, output).

        stdout+stderr are merged. On timeout the process is killed and a
        non-zero code with a ``[timeout]`` marker is returned.
        """
        process_env = dict(os.environ) if env is None else dict(env)
        if self._config.docker_host is not None:
            process_env["DOCKER_HOST"] = self._config.docker_host
        proc = await asyncio.create_subprocess_exec(
            self._config.docker_bin,
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=process_env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return 124, f"[timeout after {timeout:g}s]"
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
