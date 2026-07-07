"""Optional public telemetry: the validator publishes **aggregate** scoring
stats to a wandb project so miners and researchers can see the subnet's scoring.

Opt-in and **off by default** (``WANDB_MODE=disabled``). What is published is
aggregate-only — per-agent composite + tool/memory means + **per-category**
means, a best-per-miner leaderboard, the weight vector, and per-sweep health
scalars. It deliberately never logs raw per-case ``expected``/``called`` (the
benchmark's answer key), miner tarballs, or any key/secret. See
``ditto-platform/docs/public-telemetry.md`` for the agreed transparency policy.

The wandb import is lazy so the dependency is only needed when enabled; any
init/log failure degrades to a no-op rather than taking down the sweep loop.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ditto.api_models.validator import ScoreReport

logger = logging.getLogger(__name__)

_VALID_MODES = {"online", "offline", "disabled"}


@dataclass(frozen=True)
class TelemetryConfig:
    """wandb publishing config from ``WANDB_*`` env. Off unless online/offline."""

    mode: str
    project: str
    entity: str | None
    run_name: str | None

    @property
    def enabled(self) -> bool:
        return self.mode in ("online", "offline")


def parse_telemetry_config_from_env() -> TelemetryConfig:
    """Build the telemetry config from ``WANDB_*`` env vars (default disabled)."""
    mode = os.environ.get("WANDB_MODE", "disabled").strip().lower()
    if mode not in _VALID_MODES:
        mode = "disabled"
    return TelemetryConfig(
        mode=mode,
        project=os.environ.get("WANDB_PROJECT", "ditto-sn118"),
        entity=os.environ.get("WANDB_ENTITY") or None,
        run_name=os.environ.get("WANDB_RUN_NAME") or None,
    )


@dataclass(frozen=True)
class ScoredAgentStat:
    """One agent scored this sweep, reduced to the aggregate shape we publish."""

    miner_hotkey: str
    agent_id: str
    composite: float
    tool_mean: float
    memory_mean: float
    per_category: dict[str, float]
    n: int
    median_ms: int
    seed: int
    run_id: str
    # Aggregate telemetry from the scorer's opaque ``details`` blob (A10). Zero
    # when the scorer predates the field (older bench versions).
    bench_version: int = 0
    injection_attempts: int = 0
    paraphrase_fallbacks: int = 0


@dataclass(frozen=True)
class SweepStats:
    """Everything one sweep contributes to telemetry."""

    sweep_duration_s: float
    queue_depth: int
    failed_count: int
    scored: list[ScoredAgentStat] = field(default_factory=list)
    # (miner, composite), highest first
    leaderboard: list[tuple[str, float]] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)  # miner -> weight
    weights_submitted: bool = False


def per_category_means(report: ScoreReport) -> dict[str, float]:
    """Mean per-case score grouped by category (aggregate — no per-case detail)."""
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for case in report.per_case:
        sums[case.category] += case.score
        counts[case.category] += 1
    return {cat: sums[cat] / counts[cat] for cat in sums if counts[cat]}


def _int(value: object) -> int:
    """Coerce a details value to a non-negative int (0 on anything unexpected)."""
    try:
        n = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def scored_agent_stat(
    miner_hotkey: str, report: ScoreReport, details: dict[str, object] | None = None
) -> ScoredAgentStat:
    """Reduce a full ScoreReport (+ the scorer's opaque details) to the aggregate
    ScoredAgentStat we publish."""
    details = details or {}
    para = details.get("paraphrase")
    fallbacks = _int(para.get("fallback")) if isinstance(para, dict) else 0
    return ScoredAgentStat(
        miner_hotkey=miner_hotkey,
        agent_id=report.run_id,  # opaque handle; the real agent_id stays private
        composite=report.composite,
        tool_mean=report.tool_mean,
        memory_mean=report.memory_mean,
        per_category=per_category_means(report),
        n=report.n,
        median_ms=report.median_ms,
        seed=report.seed,
        run_id=report.run_id,
        bench_version=_int(details.get("bench_version")),
        injection_attempts=_int(details.get("injection_attempts")),
        paraphrase_fallbacks=fallbacks,
    )


class ValidatorTelemetry:
    """Publishes aggregate sweep stats to wandb. No-op when disabled/unavailable."""

    def __init__(self, config: TelemetryConfig, *, validator_hotkey: str, netuid: int):
        self._config = config
        self._validator_hotkey = validator_hotkey
        self._netuid = netuid
        self._wandb: Any = None
        self._run: Any = None
        self._step = 0
        if config.enabled:
            self._init_run()

    def _init_run(self) -> None:
        try:
            import wandb  # lazy: only needed when enabled
        except ImportError:
            logger.warning(
                "WANDB_MODE=%s but wandb is not installed; telemetry disabled "
                "(pip install wandb)",
                self._config.mode,
            )
            return
        try:
            self._run = wandb.init(
                project=self._config.project,
                entity=self._config.entity,
                name=self._config.run_name or f"validator-{self._validator_hotkey[:8]}",
                mode=self._config.mode,
                config={
                    "validator_hotkey": self._validator_hotkey,
                    "netuid": self._netuid,
                },
                reinit=False,
            )
            self._wandb = wandb
            logger.info(
                "wandb telemetry enabled: project=%s mode=%s",
                self._config.project,
                self._config.mode,
            )
        except Exception as e:  # noqa: BLE001 - never let telemetry kill the loop
            logger.warning("wandb init failed; telemetry disabled: %s", e)
            self._run = None

    @property
    def enabled(self) -> bool:
        return self._run is not None

    def record_sweep(self, stats: SweepStats) -> None:
        """Log one sweep's aggregate scalars + tables. Swallows all errors."""
        if self._run is None:
            return
        try:
            self._log_sweep(stats)
        except Exception as e:  # noqa: BLE001 - telemetry must never break scoring
            logger.warning("wandb log failed (continuing): %s", e)

    def _log_sweep(self, stats: SweepStats) -> None:
        wandb = self._wandb
        champion = (
            max(stats.weights, key=lambda m: stats.weights[m])
            if stats.weights
            else None
        )
        payload: dict[str, Any] = {
            "sweep/duration_s": stats.sweep_duration_s,
            "sweep/queue_depth": stats.queue_depth,
            "sweep/scored_count": len(stats.scored),
            "sweep/failed_count": stats.failed_count,
            "weights/miner_count": len(stats.weights),
            "weights/submitted": int(stats.weights_submitted),
            "ledger/positive_miner_count": len(stats.leaderboard),
            "ledger/champion_composite": (
                stats.leaderboard[0][1] if stats.leaderboard else 0.0
            ),
        }

        scores_tbl = wandb.Table(
            columns=[
                "miner",
                "agent",
                "composite",
                "tool_mean",
                "memory_mean",
                "n",
                "median_ms",
                "seed",
                "run_id",
                "bench_version",
                "injection_attempts",
                "paraphrase_fallbacks",
            ]
        )
        cat_tbl = wandb.Table(columns=["miner", "agent", "category", "mean"])
        for s in stats.scored:
            scores_tbl.add_data(
                s.miner_hotkey,
                s.agent_id,
                s.composite,
                s.tool_mean,
                s.memory_mean,
                s.n,
                s.median_ms,
                s.seed,
                s.run_id,
                s.bench_version,
                s.injection_attempts,
                s.paraphrase_fallbacks,
            )
            for category, mean in sorted(s.per_category.items()):
                cat_tbl.add_data(s.miner_hotkey, s.agent_id, category, mean)

        lb_tbl = wandb.Table(columns=["rank", "miner", "composite"])
        for rank, (miner, composite) in enumerate(stats.leaderboard, start=1):
            lb_tbl.add_data(rank, miner, composite)

        w_tbl = wandb.Table(columns=["miner", "weight", "role"])
        for miner, weight in sorted(stats.weights.items(), key=lambda kv: -kv[1]):
            role = "champion" if miner == champion else "tail"
            w_tbl.add_data(miner, weight, role)

        payload["scores"] = scores_tbl
        payload["category_means"] = cat_tbl
        payload["leaderboard"] = lb_tbl
        payload["weights"] = w_tbl
        wandb.log(payload, step=self._step)
        self._step += 1

    def close(self) -> None:
        if self._run is not None:
            try:
                self._run.finish()
            except Exception as e:  # noqa: BLE001
                logger.warning("wandb finish failed: %s", e)
            finally:
                self._run = None


def build_telemetry(
    config: TelemetryConfig, *, validator_hotkey: str, netuid: int
) -> ValidatorTelemetry:
    """Construct the telemetry sink (a no-op instance when disabled)."""
    return ValidatorTelemetry(config, validator_hotkey=validator_hotkey, netuid=netuid)
