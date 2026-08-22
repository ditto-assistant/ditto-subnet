"""Allowlisted prepare-report rejection codes.

The 409 body from ``/prepare-report`` previously lived only in the validator
HTTP response. These codes are the durable, low-cardinality diagnostic for
Go→Python conversion and Platform rebuild failures. They are not an exception
string channel and do not change score, retry ownership, or settlement.

Classification is type-dispatched. Interpolated ``fields drifted`` extra/missing
keys are submitter-controlled and cannot select another code.
"""

from __future__ import annotations

from ditto.api_models.confirmation_bundles import PrepareRejectionCode
from ditto.api_server.confirmation_evidence import ConfirmationEvidenceError
from ditto.api_server.confirmation_wire import ConfirmationWireError

_WIRE_EXACT: dict[str, PrepareRejectionCode] = {
    "unsupported Go ablation evidence status": "unsupported_ablation_status",
    "unsupported Go ablation evidence contract": "unsupported_ablation_contract",
    "LongMem latency drift": "longmem_latency_drift",
    "LongMem evidence targets an unsupported bench_version": (
        "unsupported_bench_version"
    ),
    "ablation evidence targets an unsupported bench_version": (
        "unsupported_bench_version"
    ),
}
_EVIDENCE_EXACT: dict[str, PrepareRejectionCode] = {
    "inference ablation profile drift": "ablation_profile_drift",
    "embedding ablation profile drift": "ablation_profile_drift",
    "inference ablation evidence digest mismatch": "ablation_digest_mismatch",
    "embedding ablation evidence digest mismatch": "ablation_digest_mismatch",
    "LongMem evidence digest mismatch": "longmem_digest_mismatch",
    "LongMem profile checksum drift": "longmem_profile_drift",
    "LongMem artifact digest drift": "longmem_profile_drift",
    "LongMem dataset identity drift": "longmem_profile_drift",
    "ablation affected-call count is not derived": "ablation_accounting",
    "LongMem envelope accounting does not equal its provider lanes": (
        "longmem_accounting"
    ),
}


def classify_prepare_rejection(error: BaseException) -> PrepareRejectionCode:
    """Map a convert/rebuild failure onto the closed ticket diagnostic."""
    if isinstance(error, ConfirmationWireError):
        message = str(error)
        if " fields drifted:" in message:
            return "go_evidence_fields_drifted"
        exact = _WIRE_EXACT.get(message)
        if exact is not None:
            return exact
        if message.endswith(" Go evidence digest mismatch"):
            return "go_evidence_digest_mismatch"
        return "confirmation_wire"
    if isinstance(error, ConfirmationEvidenceError):
        message = str(error)
        exact = _EVIDENCE_EXACT.get(message)
        if exact is not None:
            return exact
        if message.endswith(" ablation profile drift"):
            return "ablation_profile_drift"
        if message.endswith(" ablation evidence digest mismatch"):
            return "ablation_digest_mismatch"
        if "ablation" in message and any(
            token in message
            for token in ("accounting", "affected-call", "synthetic", "budget")
        ):
            return "ablation_accounting"
        if message.startswith("LongMem") and any(
            token in message for token in ("drift", "identity", "checksum", "profile")
        ):
            return "longmem_profile_drift"
        if message.startswith("LongMem") and any(
            token in message for token in ("accounting", "derived")
        ):
            return "longmem_accounting"
        return "confirmation_evidence"
    return "unclassified"
