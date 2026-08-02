"""Decline telemetry on the validator job-dispatch path.

Dispatch answers most polls ``204``, and until now every one of those looked
identical from outside. When the fleet went idle an operator could not tell
whether dispatch had *refused to issue* -- admission closed, the slot unhealthy,
the operator cap or the disk breaker -- or whether it had been willing and the
candidate walk simply found no eligible row. Answering that took
hand-reconstructing ``queue_candidate_predicate`` and ``retry_budget_spent`` as
raw SQL against production.

These tests pin the two things that make the counter trustworthy:

* the reasons **partition** the declines -- every ``204`` the handler can return
  is paired with exactly one recorded reason, so the label set sums to the
  decline rate rather than sampling it; and
* the reason attributed to a slot-cap refusal names the lever an operator would
  actually have to move.

Observability only. Nothing here may change a dispatch decision, so the
classifier is tested strictly downstream of :func:`_slot_cap_declines`.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Literal, get_args

import pytest
from prometheus_client import REGISTRY

import ditto.api_server.endpoints.validator as validator_endpoint
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.endpoints.validator import (
    _record_dispatch_decline,
    _slot_cap_decline_reason,
    _slot_cap_declines,
)
from ditto.api_server.validator_slot_settings import DISK_RESTRICTED_SLOTS
from ditto.metrics import DispatchDeclineReason

_METRIC = "ditto_validator_dispatch_declined_total"


def _declines(reason: str) -> float:
    """The counter's current value, treating a never-touched label as zero."""
    return REGISTRY.get_sample_value(_METRIC, {"reason": reason}) or 0.0


class TestReasonVocabulary:
    """The label domain is closed, and covers every gate the task named."""

    def test_the_required_reasons_are_all_present(self) -> None:
        assert {
            "slot_cap",
            "disk_breaker",
            "not_accepting",
            "slot_not_healthy",
            "no_candidate",
        } <= set(get_args(DispatchDeclineReason))

    def test_the_classifier_only_returns_declared_reasons(self) -> None:
        """A typo in a returned literal would silently open a new series."""
        settings = ValidatorSlotSettings(max_concurrent_slots=4)
        produced = {
            _slot_cap_decline_reason(
                slot_id=slot_id,
                settings=settings,
                advertised_slots=4,
                disk_percent=disk_percent,
            )
            for slot_id in ("slot-0", "slot-9", "not-a-slot")
            for disk_percent in (None, 10, 99)
        }

        assert produced <= set(get_args(DispatchDeclineReason))


class TestSlotCapDeclineReason:
    """Which of the three levers folded into ``allowed_slot_count`` refused."""

    def test_an_id_outside_the_wire_contract_is_not_a_cap_problem(self) -> None:
        """``slot-9`` is a validator-side bug; raising the cap would not help."""
        settings = ValidatorSlotSettings(max_concurrent_slots=4)

        assert (
            _slot_cap_decline_reason(
                slot_id="slot-9",
                settings=settings,
                advertised_slots=4,
                disk_percent=10,
            )
            == "slot_ceiling"
        )

    @pytest.mark.parametrize("slot_id", ["", "slot", "slot-x", "SLOT-1"])
    def test_unparseable_ids_report_the_ceiling_too(self, slot_id: str) -> None:
        settings = ValidatorSlotSettings(max_concurrent_slots=4)

        assert (
            _slot_cap_decline_reason(
                slot_id=slot_id,
                settings=settings,
                advertised_slots=4,
                disk_percent=10,
            )
            == "slot_ceiling"
        )

    def test_a_full_host_reports_the_breaker(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4, disk_percent_ceiling=90
        )

        assert (
            _slot_cap_decline_reason(
                slot_id="slot-2",
                settings=settings,
                advertised_slots=4,
                disk_percent=95,
            )
            == "disk_breaker"
        )

    def test_a_host_under_the_ceiling_reports_the_operator_cap(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=2, disk_percent_ceiling=90
        )

        assert (
            _slot_cap_decline_reason(
                slot_id="slot-2",
                settings=settings,
                advertised_slots=4,
                disk_percent=80,
            )
            == "slot_cap"
        )

    def test_unknown_disk_reports_the_operator_cap(self) -> None:
        """Absence of a sample is not evidence of a full disk, here either."""
        settings = ValidatorSlotSettings(
            max_concurrent_slots=2, disk_percent_ceiling=90
        )

        assert (
            _slot_cap_decline_reason(
                slot_id="slot-2",
                settings=settings,
                advertised_slots=4,
                disk_percent=None,
            )
            == "slot_cap"
        )

    def test_a_non_binding_breaker_is_not_blamed(self) -> None:
        """The lever that actually refused is the one worth naming.

        With the operator cap already at the disk-restricted count, clearing
        space frees nothing -- reporting ``disk_breaker`` would send whoever
        reads the dashboard to do exactly that.
        """
        settings = ValidatorSlotSettings(
            max_concurrent_slots=DISK_RESTRICTED_SLOTS,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=100,
        )

        assert (
            _slot_cap_decline_reason(
                slot_id="slot-1",
                settings=settings,
                advertised_slots=4,
                disk_percent=92,
            )
            == "slot_cap"
        )

    def test_a_validator_offering_one_slot_is_not_blamed_on_the_disk(self) -> None:
        """The cap can only narrow the offer; a one-slot host is at its own limit."""
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=100,
        )

        assert (
            _slot_cap_decline_reason(
                slot_id="slot-0",
                settings=settings,
                advertised_slots=1,
                disk_percent=92,
            )
            == "slot_cap"
        )

    def test_the_classifier_never_moves_the_decision(self) -> None:
        """Same inputs, same verdict, whether or not a reason was asked for."""
        settings = ValidatorSlotSettings(max_concurrent_slots=2)
        held = {"slot-0", "slot-1"}

        declined = _slot_cap_declines(
            slot_id="slot-2",
            slot_running_benchmark=False,
            allowed_slots=2,
            held_slots=held,
        )
        _slot_cap_decline_reason(
            slot_id="slot-2",
            settings=settings,
            advertised_slots=4,
            disk_percent=50,
        )

        assert declined
        assert _slot_cap_declines(
            slot_id="slot-2",
            slot_running_benchmark=False,
            allowed_slots=2,
            held_slots=held,
        )


class TestRecordDispatchDecline:
    def test_it_increments_the_labelled_counter(self) -> None:
        before = _declines("slot_cap")

        _record_dispatch_decline(
            "slot_cap", validator_hotkey="5Hotkey", slot_id="slot-3"
        )

        assert _declines("slot_cap") == before + 1

    def test_reasons_do_not_bleed_into_each_other(self) -> None:
        before = _declines("no_candidate")

        _record_dispatch_decline(
            "slot_cap", validator_hotkey="5Hotkey", slot_id="slot-3"
        )

        assert _declines("no_candidate") == before

    def test_the_log_line_carries_what_the_counter_cannot(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Hotkey and slot are too high-cardinality for a label, so they land here."""
        with caplog.at_level(
            logging.INFO, logger="ditto.api_server.endpoints.validator"
        ):
            _record_dispatch_decline(
                "disk_breaker",
                validator_hotkey="5DiskFullValidator",
                slot_id="slot-2",
            )

        message = caplog.records[-1].getMessage()
        assert "reason=disk_breaker" in message
        assert "5DiskFullValidator" in message
        assert "slot-2" in message


class TestEveryDeclineIsAccountedFor:
    """No silent 204s: the counter partitions the declines, it does not sample.

    Read structurally rather than by exercising each branch, because several of
    these gates need a whole production-shaped fixture (an open rollout, a stale
    cross-era lease) to reach. What would actually rot is somebody adding a
    sixth ``return Response(status_code=204)`` later and leaving it dark -- and
    that is exactly what this catches.
    """

    def _dispatch_source(self) -> ast.AsyncFunctionDef:
        source = Path(validator_endpoint.__file__).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "request_job":
                return node
        raise AssertionError("request_job not found")

    def _is_204(self, statement: ast.stmt) -> bool:
        return (
            isinstance(statement, ast.Return)
            and isinstance(statement.value, ast.Call)
            and any(
                keyword.arg == "status_code"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == 204
                for keyword in statement.value.keywords
            )
        )

    def _records(self, statement: ast.stmt) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_record_dispatch_decline"
            for node in ast.walk(statement)
        )

    def _unrecorded_declines(self, node: ast.AST) -> list[int]:
        """Lines where a 204 is returned with no reason recorded beside it.

        "Beside it" is a preceding sibling in the enclosing block, not a
        line-distance window -- a window would break the first time one of
        these call sites grew a line.
        """
        unrecorded: list[int] = []
        for parent in ast.walk(node):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(parent, field, None)
                if not isinstance(block, list):
                    continue
                for index, statement in enumerate(block):
                    if self._is_204(statement) and not any(
                        self._records(earlier) for earlier in block[:index]
                    ):
                        unrecorded.append(statement.lineno)
        return unrecorded

    def test_the_handler_still_has_declines_to_account_for(self) -> None:
        dispatch = self._dispatch_source()

        assert [
            statement.lineno
            for statement in ast.walk(dispatch)
            if isinstance(statement, ast.stmt) and self._is_204(statement)
        ]

    def test_every_204_records_a_reason_first(self) -> None:
        unrecorded = self._unrecorded_declines(self._dispatch_source())

        assert unrecorded == [], (
            f"{validator_endpoint.__file__} returns 204 without recording a "
            f"decline reason at line(s) {unrecorded}"
        )


class TestDeclineReasonIsAWireLevelLiteral:
    def test_the_alias_is_a_literal_of_strings(self) -> None:
        """A stringly-typed reason would let a caller invent a label."""
        assert getattr(DispatchDeclineReason, "__origin__", None) is Literal
        assert all(isinstance(value, str) for value in get_args(DispatchDeclineReason))
