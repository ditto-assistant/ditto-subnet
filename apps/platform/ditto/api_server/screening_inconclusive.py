"""Route a screener INCONCLUSIVE outcome to operator review or to a retry.

A typed ``inconclusive`` outcome reaches Platform from two different causes:

* **A reviewer non-verdict.** The L1/L2/L3 source review (or an explicit
  challenge) ran and could not decide either way: ``l2-model-inconclusive``,
  ``source-review-inconclusive``, ``l3-adjudicator-inconclusive``, a critic or
  adjudicator disagreement, an unresolved violation lead, or a bounded review
  budget that stopped on its notes ledger. Retrying the same artifact under the
  same policy has no reason to conclude differently, so the attempt is held as
  an active screening quarantine (``AgentStatus.QUARANTINED``) and an operator
  releases, rejects, or rescreens it from Backroom.
* **An infrastructure fault carried as a hold.** The worker turns an L2/L3
  provider or transport failure into ``inconclusive`` whenever an earlier
  stage already recorded a finding (``l2_review._carry_l1_notes``), and the
  signed-runtime preflight reports ``l2-runtime-evidence-unavailable`` as
  ``inconclusive``. Those reason codes name a fault, not a verdict, so they
  stay on the retry path: the attempt parks in ``SCREENING_FAILED`` exactly as
  before for an operator-authorized retry.

The split is a deny list of fault shapes rather than an allow list of verdict
codes because the worker composes most codes dynamically
(``l3-violation-adjudicator-<trajectory code>``, ``<stage>-http-<status>``,
``<stage>-<exception name>``). A fault is recognized from the shared screening
registry's infrastructure classes plus the transport markers the worker's own
code builders emit. Anything else, including a missing code, is a reviewer
non-verdict and is held for a human: holding a stray fault costs one operator
``rescreen``, while retrying a real non-verdict would hide it from the queue.
"""

from __future__ import annotations

import re
from typing import Final

from ditto_screening_protocol.reason_codes import ReasonCodeClass, reason_code_class

INCONCLUSIVE_OPERATOR_REVIEW_PUBLIC_REASON: Final = (
    "Screening was inconclusive; held for operator review"
)
"""Miner-visible reason for a reviewer non-verdict held in the quarantine court."""

INCONCLUSIVE_RETRY_PUBLIC_REASON: Final = (
    "Screening was inconclusive; manual retry required"
)
"""Miner-visible reason for an infrastructure fault carried as ``inconclusive``."""

_INFRASTRUCTURE_CLASSES: Final = frozenset(
    {
        ReasonCodeClass.FLEET_INFRA,
        ReasonCodeClass.PROVIDER_BACKOFF,
        ReasonCodeClass.OPERATOR_RETRY,
    }
)

# Fault markers the worker's code builders emit (``l2_review._error_code``,
# ``source_review._source_review_failure_code``, ``L2TrajectoryError`` codes,
# the behavioral oracle's transport failure). Each matches whole hyphen
# segments so a verdict word can never be read as a fault by accident.
_FAULT_SEGMENTS: Final = re.compile(
    r"(?:^|-)(?:"
    r"provider-fault"
    r"|response-contract"
    r"|credits-exhausted"
    r"|transport-failure"
    r"|analyzer-contract"
    r"|analyzer-exited"
    r"|http-\d{3}"
    r")(?=-|$)"
)
# ``_error_code`` falls back to the lowercased exception class name
# (``connecterror``, ``readtimeout``, ``remoteprotocolerror``, ``oserror``).
_FAULT_TAIL: Final = re.compile(r"(?:timeout|error|exception)$")


def inconclusive_is_infrastructure_fault(reason_code: str | None) -> bool:
    """True when an INCONCLUSIVE outcome's code names a fault, not a verdict."""
    if reason_code is None:
        return False
    if reason_code_class(reason_code) in _INFRASTRUCTURE_CLASSES:
        return True
    if _FAULT_SEGMENTS.search(reason_code) is not None:
        return True
    return _FAULT_TAIL.search(reason_code) is not None


def inconclusive_requires_operator_review(reason_code: str | None) -> bool:
    """True when an INCONCLUSIVE outcome is a reviewer non-verdict to hold."""
    return not inconclusive_is_infrastructure_fault(reason_code)
