"""Typed errors for the validator worker."""

from __future__ import annotations


class ValidatorError(Exception):
    """Base class for validator-worker failures."""


class ValidatorConfigError(ValidatorError):
    """Raised when the worker is misconfigured (missing/invalid env)."""


class DittobenchError(ValidatorError):
    """Raised when a dittobench-api run fails or times out."""


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


class SandboxOomError(DittobenchError):
    """The miner sandbox exceeded its bounded memory allowance.

    This is an artifact-specific terminal attempt outcome, not evidence that
    the validator's shared infrastructure is unhealthy. The worker hands the
    lease back as ``sandbox_oom`` and continues with another harness without
    disabling the execution slot.
    """


class PlatformError(ValidatorError):
    """Raised when a platform ``/validator/*`` call fails."""


class WeightSubmissionError(ValidatorError):
    """Raised when an on-chain weight submission fails (SDK fallback path)."""
