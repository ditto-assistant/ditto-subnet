"""Allowlisted prepare-report rejection codes.

The 409 body from ``/prepare-report`` previously lived only in the validator
HTTP response. These codes are the durable, low-cardinality diagnostic for
Go→Python conversion and Platform rebuild failures. They are not an exception
string channel and do not change score, retry ownership, or settlement.
"""

from __future__ import annotations

from ditto.api_models.confirmation_bundles import PrepareRejectionCode
from ditto.api_server.confirmation_evidence import ConfirmationEvidenceError
from ditto.api_server.confirmation_wire import ConfirmationWireError


def classify_prepare_rejection(error: BaseException) -> PrepareRejectionCode:
    """Map a convert/rebuild failure onto the closed ticket diagnostic."""
    message = str(error)
    if "Go evidence digest mismatch" in message:
        return "go_evidence_digest_mismatch"
    if "fields drifted" in message:
        return "go_evidence_fields_drifted"
    if "unsupported Go ablation evidence status" in message:
        return "unsupported_ablation_status"
    if "unsupported Go ablation evidence contract" in message:
        return "unsupported_ablation_contract"
    if "ablation profile drift" in message:
        return "ablation_profile_drift"
    if "ablation evidence digest mismatch" in message:
        return "ablation_digest_mismatch"
    if message == "LongMem evidence digest mismatch":
        return "longmem_digest_mismatch"
    if message == "LongMem latency drift":
        return "longmem_latency_drift"
    if "unsupported bench_version" in message:
        return "unsupported_bench_version"
    if "ablation" in message and any(
        token in message
        for token in (
            "accounting",
            "affected-call",
            "synthetic",
            "budget",
        )
    ):
        return "ablation_accounting"
    if message.startswith("LongMem") or "LongMem" in message:
        if any(
            token in message for token in ("drift", "identity", "checksum", "profile")
        ):
            return "longmem_profile_drift"
        if any(token in message for token in ("accounting", "derived")):
            return "longmem_accounting"
    if isinstance(error, ConfirmationWireError):
        return "confirmation_wire"
    if isinstance(error, ConfirmationEvidenceError):
        return "confirmation_evidence"
    return "unclassified"
