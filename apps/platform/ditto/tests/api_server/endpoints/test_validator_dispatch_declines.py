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
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast, get_args
from uuid import UUID

import pytest
from prometheus_client import REGISTRY

import ditto.api_server.endpoints.validator as validator_endpoint
from ditto.api_models.benchmark_capacity import ActiveBenchmarkSlot, BenchmarkCapacity
from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    BenchmarkProgressStage,
)
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.endpoints.validator import (
    _inference_stage_cap_declines,
    _inference_stage_slot_cap,
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
            "inference_slot_cap",
            "provider_outage",
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


class TestInferenceStageSlotCap:
    """Hosted embedding capacity bounds new post-v7 leases."""

    @staticmethod
    def _config(*, per_ticket: int, per_validator: int) -> Any:
        return cast(
            Any,
            SimpleNamespace(
                embedding_per_ticket_concurrency=per_ticket,
                embedding_per_validator_concurrency=per_validator,
            ),
        )

    def test_one_lane_cannot_be_stampeded_by_eight_leases(self) -> None:
        assert (
            _inference_stage_slot_cap(self._config(per_ticket=1, per_validator=1)) == 1
        )

    def test_validator_capacity_is_divided_by_each_ticket_allowance(self) -> None:
        assert (
            _inference_stage_slot_cap(self._config(per_ticket=2, per_validator=8)) == 4
        )

    def test_cap_never_exceeds_the_slot_wire_contract(self) -> None:
        assert (
            _inference_stage_slot_cap(self._config(per_ticket=1, per_validator=1_000))
            == 8
        )

    def test_invalid_zero_configuration_still_fails_bounded(self) -> None:
        assert (
            _inference_stage_slot_cap(self._config(per_ticket=0, per_validator=0)) == 1
        )

    @staticmethod
    def _active(
        slot_id: str, *, stage: str | None, bench_version: int = 9
    ) -> ActiveBenchmarkSlot:
        return ActiveBenchmarkSlot(
            slot_id=slot_id,
            agent_id=UUID("00000000-0000-0000-0000-000000000001"),
            bench_version=bench_version,
            progress=(
                None
                if stage is None
                else BenchmarkProgress(
                    stage=cast(BenchmarkProgressStage, stage),
                    completed=1 if stage == "running_benchmark" else None,
                    total=10 if stage == "running_benchmark" else None,
                    ticket_deadline=datetime(2026, 8, 14, 7, tzinfo=UTC),
                )
            ),
        )

    def test_startup_work_consumes_the_stage_cap(self) -> None:
        capacity = BenchmarkCapacity(
            configured_slots=4,
            healthy_slots=["slot-0", "slot-1", "slot-2", "slot-3"],
            active=[
                self._active("slot-0", stage="generating_dataset"),
                self._active("slot-1", stage="starting_harness"),
            ],
        )

        assert _inference_stage_cap_declines(
            slot_id="slot-2",
            slot_running_benchmark=False,
            allowed_slots=2,
            capacity=capacity,
        )

    def test_grading_work_does_not_waste_startup_capacity(self) -> None:
        capacity = BenchmarkCapacity(
            configured_slots=4,
            healthy_slots=["slot-0", "slot-1", "slot-2", "slot-3"],
            active=[
                self._active("slot-0", stage="running_benchmark"),
                self._active("slot-1", stage="running_benchmark"),
            ],
        )

        assert not _inference_stage_cap_declines(
            slot_id="slot-2",
            slot_running_benchmark=False,
            allowed_slots=1,
            capacity=capacity,
        )

    def test_unreported_active_progress_is_conservatively_startup(self) -> None:
        capacity = BenchmarkCapacity(
            configured_slots=2,
            healthy_slots=["slot-0", "slot-1"],
            active=[self._active("slot-0", stage=None)],
        )

        assert _inference_stage_cap_declines(
            slot_id="slot-1",
            slot_running_benchmark=False,
            allowed_slots=1,
            capacity=capacity,
        )

    def test_pre_v7_work_does_not_consume_hosted_embedding_capacity(self) -> None:
        capacity = BenchmarkCapacity(
            configured_slots=2,
            healthy_slots=["slot-0", "slot-1"],
            active=[self._active("slot-0", stage=None, bench_version=6)],
        )

        assert not _inference_stage_cap_declines(
            slot_id="slot-1",
            slot_running_benchmark=False,
            allowed_slots=1,
            capacity=capacity,
        )

    def test_resume_is_never_stranded_by_a_lower_live_cap(self) -> None:
        capacity = BenchmarkCapacity(
            configured_slots=2,
            healthy_slots=["slot-0", "slot-1"],
            active=[
                self._active("slot-0", stage="starting_harness"),
                self._active("slot-1", stage="starting_harness"),
            ],
        )

        assert not _inference_stage_cap_declines(
            slot_id="slot-1",
            slot_running_benchmark=True,
            allowed_slots=1,
            capacity=capacity,
        )


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
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hotkey and slot are too high-cardinality for a label, so they land here."""
        dispatch_logger = logging.getLogger("ditto.api_server.endpoints.validator")
        # Reproduce the xdist order where another test's logging setup leaves an
        # already-imported named logger disabled in this worker.
        monkeypatch.setattr(dispatch_logger, "disabled", True)
        was_disabled = dispatch_logger.disabled
        dispatch_logger.disabled = False
        dispatch_logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger=dispatch_logger.name):
                _record_dispatch_decline(
                    "disk_breaker",
                    validator_hotkey="5DiskFullValidator",
                    slot_id="slot-2",
                )
        finally:
            dispatch_logger.removeHandler(caplog.handler)
            dispatch_logger.disabled = was_disabled

        message = next(
            record.getMessage()
            for record in caplog.records
            if "declined job reason=disk_breaker" in record.getMessage()
        )
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
