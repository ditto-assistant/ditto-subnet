"""Compatibility exports for the shared Bench v9 confirmation wire adapter."""

from ditto_screening_protocol.confirmation_wire import (
    ConfirmationWireError,
    ablation_envelope_from_go,
    completion_report_from_go_dimensions,
    completion_report_from_go_fixture,
    longmem_envelope_from_go,
)

__all__ = [
    "ConfirmationWireError",
    "ablation_envelope_from_go",
    "completion_report_from_go_dimensions",
    "completion_report_from_go_fixture",
    "longmem_envelope_from_go",
]
