"""Unit tests for :mod:`ditto.validator.telemetry`.

The public telemetry sink is **opt-in** (``WANDB_MODE=disabled`` by default) and
must degrade to a cheap no-op when disabled or when wandb is not installed. What
it reduces a ``ScoreReport`` to is **aggregate-only** — per-category means, never
the per-case answer key. These tests cover config parsing, the aggregate
reduction, and the no-op guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
        latency_ms=100,
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
        # rather than taking down the sweep loop.
        import builtins

        real_import = builtins.__import__

        def _fail(name: str, *args: object, **kwargs: object) -> object:
            if name == "wandb":
                raise ImportError("no wandb")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail)
        telemetry = build_telemetry(
            TelemetryConfig(mode="online", project="p", entity=None, run_name=None),
            validator_hotkey=_VALIDATOR,
            netuid=118,
        )
        assert telemetry.enabled is False
