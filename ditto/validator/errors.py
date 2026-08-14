"""Typed errors for the validator worker."""

from __future__ import annotations


class ValidatorError(Exception):
    """Base class for validator-worker failures."""


class ValidatorConfigError(ValidatorError):
    """Raised when the worker is misconfigured (missing/invalid env)."""


class DittobenchError(ValidatorError):
    """Raised when a dittobench-api run fails or times out.

    ``code`` is the scorer's own machine-readable failure code when the payload
    carried one (see ``dittobench._SANDBOX_INFRASTRUCTURE_CODES``). It exists
    because that code used to die here: the worker collapsed all five sandbox
    infrastructure codes into one unlabelled ``infrastructure`` hand-back and the
    specific code survived only in a log line on the validator host. ditto-subnet#279
    read twelve dead ``mnemo*`` leases off the ticket ledger and still could not
    name what killed them at ~60 minutes, for that reason alone. Now it rides the
    wire in ``FailJobRequest.failure_detail`` and lands on the ticket.

    ``container_log_tail`` is the failing harness's own bounded, redacted stdout
    and stderr, lifted off the scorer's failure envelope. It answers the question
    ``code`` cannot: *why* the harness died, in the harness's own words. The
    scorer has captured it since the sandbox log-tail change but nothing read it,
    so it reached an operator only by shelling into whichever validator host ran
    the job -- against an in-memory job store, so in practice not at all. Agent
    ``5fdadd33`` burned four leases in 82-108s each with nothing readable behind
    ``scoring_error``, which is this field's reason for existing. Kept separate
    from ``code`` and from the message so neither becomes prose: ``code`` is what
    an operator groups by, this is what they read.
    """

    def __init__(
        self,
        *args: object,
        code: str | None = None,
        container_log_tail: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.code = code
        self.container_log_tail = container_log_tail


class ValidatorInfrastructureError(DittobenchError):
    """Retryable failure in validator-owned scoring infrastructure.

    Unlike an ordinary benchmark failure, this says nothing about the miner's
    artifact. The current scoring sweep must stop and let any issued lease
    expire so the submission remains eligible for another validator attempt.
    """


class LeaseDeadlineError(DittobenchError):
    """The attempt ran out of lease before the scorer returned a verdict.

    Terminal for this attempt, and deliberately **not** a
    :class:`ValidatorInfrastructureError`. A run that was given the whole lease
    and still produced nothing says something about the artifact, not about
    this host. Reporting it as ``infrastructure`` would mint a no-fault retry
    grant, apply the escalating infra backoff, and re-lease the same hanging
    artifact indefinitely — the loop that let one miner family hold three
    validator slots per round without ever reaching a verdict. It is handed
    back as ``scoring_error`` instead, so the attempt is consumed and the agent
    reaches a terminal outcome.
    """


class LeaseRevokedError(ValidatorError):
    """The platform authoritatively stopped holding the lease this run needed.

    Not a failure of the artifact, of this host, or of the run. The platform owns
    lease assignment; when its heartbeat roster stops listing a lease (an
    operator eviction, most often), continuing to score is pure waste — a full
    host slot burned for up to the lease TTL to produce a score the platform will
    refuse with a clean 409.

    Deliberately **not** a :class:`DittobenchError`, so it cannot be swallowed by
    the ``(DittobenchError, PlatformError)`` hand-back and cannot be reordered
    into or out of the :class:`LeaseDeadlineError` branch by accident. It gets
    its own handler that reports *nothing* back: there is no ticket left to hand
    back, ``scoring_error`` would consume an attempt the platform already took
    away, and ``infrastructure`` would mint a no-fault grant and re-lease the
    submission forever.
    """


class SandboxOomError(DittobenchError):
    """The miner sandbox exceeded its bounded memory allowance.

    This is an artifact-specific terminal attempt outcome, not evidence that
    the validator's shared infrastructure is unhealthy. The worker hands the
    lease back as ``sandbox_oom`` and continues with another harness without
    disabling the execution slot.
    """


class PlatformError(ValidatorError):
    """Raised when a platform ``/validator/*`` call fails."""


class PlatformInfrastructureError(PlatformError):
    """A retryable platform dependency failure unrelated to the submission."""


FAILURE_DETAIL_MAX_LENGTH = 4096
"""Mirrors ``ditto.api_models.validator.FAILURE_DETAIL_MAX_LENGTH``.

Duplicated rather than imported so this module keeps its zero-dependency shape;
:func:`failure_detail` asserts nothing about it beyond truncating to it, and the
wire model is the enforcing side.
"""

TRUNCATION_MARKER_TEMPLATE = "...[truncated, {total} chars]"
"""Appended when :func:`truncate_failure_detail` actually cuts something.

The old 200-char cap cut

    ``... the platform rejected 81 of the harness's inference r``

and said nothing about having done so. What made that expensive was not the
lost characters but the lost *signal*: the fragment ended on a word boundary
that read as a complete finding, so there was no reason to suspect a second
clause existed. A reader who sees this marker knows to go looking; a reader who
does not can trust what they are holding is whole.

Carries the pre-truncation length because "how much is missing" is the first
question asked next, and it is free to record here and unrecoverable later.
"""


def truncate_failure_detail(detail: str, limit: int = FAILURE_DETAIL_MAX_LENGTH) -> str:
    """Cut ``detail`` to ``limit`` characters, visibly.

    Returns ``detail`` unchanged when it fits -- the overwhelmingly common case,
    and one that must not acquire a marker it hasn't earned. Otherwise the tail
    is replaced with :data:`TRUNCATION_MARKER_TEMPLATE` so the result is still
    within ``limit`` *including* the marker: the point is to satisfy the wire
    bound, so a marker that pushed the value back over it would defeat itself.

    ``limit`` is a parameter rather than a constant read because the client
    re-truncates to the legacy bound when talking to an un-upgraded platform.
    """
    if len(detail) <= limit:
        return detail
    marker = TRUNCATION_MARKER_TEMPLATE.format(total=len(detail))
    if limit <= len(marker):
        # Degenerate limit: no room for both. The marker alone is more useful
        # than a prefix that lies about being whole, so keep as much of it as
        # fits. Unreachable at either real bound (200 and 4096 both dwarf it).
        return marker[:limit]
    return detail[: limit - len(marker)] + marker


def failure_detail(error: BaseException) -> str:
    """Bounded, machine-first description of why an attempt died.

    Prefers the scorer's structured code, because that is the field an operator
    can group by and the thing ditto-subnet#279 could not recover. Falls back to
    the exception type and message, which still beats the status quo of a
    three-value class and nothing else. That precedence is deliberate and load
    bearing -- the code is what gets grouped and classified, the message is the
    human-facing detail -- and widening the cap does not disturb it: a code is
    short enough that the bound never applied to it in the first place. What the
    wider bound buys is the *fallback* branch, which is where real diagnostic
    messages live and where 200 characters was demonstrably not enough.

    Always truncates to :data:`FAILURE_DETAIL_MAX_LENGTH`. Sending an overlong
    detail would be rejected 422 by the platform and lose the whole hand-back,
    turning a diagnosis into the silent expiry the field exists to prevent -- so
    the cap is enforced here, on the sending side, not merely respected. Since
    it goes through :func:`truncate_failure_detail`, a cut now announces itself.
    """
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return truncate_failure_detail(code.strip())
    message = str(error).strip()
    detail = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return truncate_failure_detail(detail)


CONTAINER_LOG_TAIL_MAX_LENGTH = 2048
"""Mirrors ``ditto.api_models.validator.CONTAINER_LOG_TAIL_MAX_LENGTH``.

The scorer already bounds the tail at ``sandbox.ContainerLogTailBytes`` (2000)
after pre-bounding it at the Docker daemon with ``--tail 500``. This is the same
bound plus headroom for the truncation marker, enforced again here for the same
reason :func:`failure_detail` enforces its own: an overlong field is rejected 422
and loses the whole hand-back, turning a diagnosis into a silent expiry.

Duplicated rather than imported to keep this module dependency-free, exactly as
:data:`FAILURE_DETAIL_MAX_LENGTH` is. The wire model is the enforcing side.
"""


def container_log_tail(error: BaseException) -> str | None:
    """The failing harness's own output, bounded for the wire.

    Returns ``None`` when the error carries no tail -- a scorer that predates the
    field, a failure raised before any container existed, or a container that
    produced nothing. ``None`` and ``""`` are deliberately not merged: the scorer
    returns ``""`` for "the runtime could not be queried", and a field that is
    absent says that more honestly than an empty string that reads as "the
    harness printed nothing".
    """
    tail = getattr(error, "container_log_tail", None)
    if not isinstance(tail, str):
        return None
    tail = tail.strip()
    if not tail:
        return None
    return truncate_failure_detail(tail, CONTAINER_LOG_TAIL_MAX_LENGTH)


class WeightSubmissionError(ValidatorError):
    """Raised when an on-chain weight submission fails (SDK fallback path)."""
