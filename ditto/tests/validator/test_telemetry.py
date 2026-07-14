"""Unit tests for :mod:`ditto.validator.telemetry`.

The public telemetry sink is **opt-in** (``WANDB_MODE=disabled`` by default) and
must degrade to a cheap no-op when disabled or when wandb is not installed. What
it reduces a ``ScoreReport`` to is **aggregate-only** — per-category means, never
the per-case answer key. These tests cover config parsing, the aggregate
reduction, and the no-op guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ditto.api_models.validator import CaseScore, ScoreReport
from ditto.validator.telemetry import (
    SweepStats,
    TelemetryConfig,
    ValidatorTelemetry,
    build_telemetry,
    parse_telemetry_config_from_env,
    per_category_means,
    scored_agent_stat,
)

_VALIDATOR = "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"


def _case(category: str, score: float) -> CaseScore:
    return CaseScore(
        case_id=f"{category}-{score}",
        category=category,
        kind="tool",
        score=score,
        tool_score=score,
        quality=0.0,
        correct=False,
        latency_ms=100,
        called=[],
        expected=[],
        notes=[],
    )


def _report(**over: object) -> ScoreReport:
    base: dict[str, object] = {
        "run_id": "run_1",
        "seed": 42,
        "composite": 0.8,
        "tool_mean": 0.85,
        "memory_mean": 0.72,
        "median_ms": 500,
        "n": 4,
        "generated_at": datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
        "per_case": [
            _case("web_search", 0.9),
            _case("web_search", 0.7),
            _case("recall", 0.6),
            _case("recall", 1.0),
        ],
    }
    base.update(over)
    return ScoreReport(**base)  # type: ignore[arg-type]


class TestParseConfig:
    def test_defaults_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("WANDB_MODE", "WANDB_PROJECT", "WANDB_ENTITY", "WANDB_RUN_NAME"):
            monkeypatch.delenv(var, raising=False)
        config = parse_telemetry_config_from_env()
        assert config.mode == "disabled"
        assert config.enabled is False
        assert config.project == "ditto-sn118"
        assert config.entity is None

    def test_online_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WANDB_MODE", "online")
        monkeypatch.setenv("WANDB_PROJECT", "my-proj")
        monkeypatch.setenv("WANDB_ENTITY", "my-team")
        config = parse_telemetry_config_from_env()
        assert config.mode == "online"
        assert config.enabled is True
        assert config.project == "my-proj"
        assert config.entity == "my-team"

    def test_unknown_mode_falls_back_to_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WANDB_MODE", "bogus")
        config = parse_telemetry_config_from_env()
        assert config.mode == "disabled"
        assert config.enabled is False


class TestAggregateReduction:
    def test_per_category_means(self) -> None:
        means = per_category_means(_report())
        assert means == pytest.approx({"web_search": 0.8, "recall": 0.8})

    def test_empty_per_case(self) -> None:
        assert per_category_means(_report(per_case=[])) == {}

    def test_scored_agent_stat_is_aggregate_only(self) -> None:
        # agent_id is the opaque run_id (real agent_id stays private) and only
        # per-category means are carried — never the per-case answer key.
        stat = scored_agent_stat("5Miner", _report())
        assert stat.miner_hotkey == "5Miner"
        assert stat.agent_id == "run_1"  # == run_id, not a real agent uuid
        assert stat.run_id == "run_1"
        assert stat.composite == pytest.approx(0.8)
        assert stat.tool_mean == pytest.approx(0.85)
        assert stat.memory_mean == pytest.approx(0.72)
        assert stat.per_category == pytest.approx({"web_search": 0.8, "recall": 0.8})
        assert stat.n == 4
        # No field carries per-case expected/called (the answer key).
        assert not hasattr(stat, "per_case")

    def test_scored_agent_stat_carries_details_telemetry(self) -> None:
        # The scorer's opaque details blob surfaces as aggregate scalars.
        details = {
            "bench_version": 2,
            "injection_attempts": 3,
            "paraphrase": {"attempted": 40, "applied": 35, "fallback": 5},
            # Observed-execution telemetry.
            "observed_tool_cases": 12,
            "capped_tool_cases": 2,
            "isolation_cases": 4,
        }
        stat = scored_agent_stat("5Miner", _report(), details)
        assert stat.bench_version == 2
        assert stat.injection_attempts == 3
        assert stat.paraphrase_fallbacks == 5
        assert stat.observed_tool_cases == 12
        assert stat.capped_tool_cases == 2
        assert stat.isolation_cases == 4

    def test_scored_agent_stat_defaults_without_details(self) -> None:
        # Older scorers (no details) default to zeros, never crash.
        stat = scored_agent_stat("5Miner", _report())
        assert stat.bench_version == 0
        assert stat.injection_attempts == 0
        assert stat.paraphrase_fallbacks == 0
        assert stat.observed_tool_cases == 0
        assert stat.capped_tool_cases == 0
        assert stat.isolation_cases == 0
        # Malformed details are coerced, not fatal.
        weird = scored_agent_stat("5Miner", _report(), {"bench_version": "oops"})
        assert weird.bench_version == 0


class TestWeightStatusTelemetry:
    class _Table:
        def __init__(self, *, columns: list[str]) -> None:
            self.columns = columns
            self.rows: list[tuple[object, ...]] = []

        def add_data(self, *values: object) -> None:
            self.rows.append(values)

    def _telemetry(self) -> tuple[ValidatorTelemetry, list[dict[str, object]]]:
        logged: list[dict[str, object]] = []
        telemetry = ValidatorTelemetry(
            TelemetryConfig(mode="disabled", project="p", entity=None, run_name=None),
            validator_hotkey=_VALIDATOR,
            netuid=118,
        )
        telemetry._wandb = SimpleNamespace(  # type: ignore[attr-defined]
            Table=self._Table,
            log=lambda payload, **_kwargs: logged.append(payload),
        )
        telemetry._run = object()  # type: ignore[attr-defined]
        return telemetry, logged

    def test_safe_idle_submission_is_explicit_and_persists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        telemetry, logged = self._telemetry()
        monkeypatch.setattr("ditto.validator.telemetry.time.time", lambda: 1000.0)

        telemetry.record_sweep(
            SweepStats(
                sweep_duration_s=1.0,
                queue_depth=0,
                failed_count=0,
                weights={_VALIDATOR: 1.0},
                weights_submitted=True,
                weights_due=True,
                burn_hotkey=_VALIDATOR,
            )
        )

        due = logged[-1]
        assert due["weights/status"] == "safe_idle"
        assert due["weights/submitted"] == 1
        assert due["weights/idle_burn"] == 1
        assert due["weights/miner_count"] == 0
        assert due["weights/burn_share"] == 1.0
        weights_table = due["weights"]
        assert isinstance(weights_table, self._Table)
        assert weights_table.rows == [(_VALIDATOR, 1.0, "burn")]

        monkeypatch.setattr("ditto.validator.telemetry.time.time", lambda: 1120.0)
        telemetry.record_sweep(
            SweepStats(sweep_duration_s=1.0, queue_depth=0, failed_count=0)
        )

        ordinary = logged[-1]
        assert "weights/submitted" not in ordinary
        assert "weights/status" not in ordinary
        assert "weights" not in ordinary
        assert "leaderboard" not in ordinary
        assert ordinary["weights/last_success_age_seconds"] == 120.0

    def test_due_failure_is_not_reported_as_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        telemetry, logged = self._telemetry()
        monkeypatch.setattr("ditto.validator.telemetry.time.time", lambda: 1000.0)

        telemetry.record_sweep(
            SweepStats(
                sweep_duration_s=1.0,
                queue_depth=0,
                failed_count=1,
                weights={_VALIDATOR: 1.0},
                weights_submitted=False,
                weights_due=True,
                burn_hotkey=_VALIDATOR,
            )
        )

        assert logged[-1]["weights/status"] == "failed"
        assert logged[-1]["weights/submitted"] == 0
        assert logged[-1]["weights/idle_burn"] == 0
        assert "weights/last_success_age_seconds" not in logged[-1]


class TestNoOpWhenDisabled:
    def _disabled(self) -> ValidatorTelemetry:
        return build_telemetry(
            TelemetryConfig(mode="disabled", project="p", entity=None, run_name=None),
            validator_hotkey=_VALIDATOR,
            netuid=118,
        )

    def test_disabled_never_initialises_a_run(self) -> None:
        telemetry = self._disabled()
        assert telemetry.enabled is False

    def test_record_and_close_are_noops(self) -> None:
        telemetry = self._disabled()
        # Must not raise even though no wandb run exists.
        telemetry.record_sweep(
            SweepStats(
                sweep_duration_s=1.0,
                queue_depth=0,
                failed_count=0,
                scored=[scored_agent_stat("5Miner", _report())],
                leaderboard=[("5Miner", 0.8)],
                weights={"5Miner": 1.0},
                weights_submitted=True,
            )
        )
        telemetry.close()

    def test_missing_wandb_degrades_to_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even with mode=online, an ImportError on wandb leaves it disabled
        # rather than taking down the sweep loop. A ``None`` entry in
        # ``sys.modules`` makes ``import wandb`` raise ImportError — the exact
        # path _init_run guards — without touching ``builtins.__import__``.
        import sys

        monkeypatch.setitem(sys.modules, "wandb", None)
        telemetry = build_telemetry(
            TelemetryConfig(mode="online", project="p", entity=None, run_name=None),
            validator_hotkey=_VALIDATOR,
            netuid=118,
        )
        assert telemetry.enabled is False
