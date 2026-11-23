"""Backtest the SN-500 P95 harness-memory factor on exported season scores.

Input: a CSV with one row per (season, agent, validator) score that has a
matching harness memory observation, columns:

    season,agent,validator,composite,p95_bytes,peak_bytes,limit_bytes

exported read-only from Platform, e.g.

    SELECT d.season_index, left(s.agent_id::text, 8), left(s.validator_hotkey, 6),
           s.composite, o.p95_bytes, o.peak_bytes, o.limit_bytes
      FROM dueling_seasons d
      JOIN scores s ON s.dueling_season_id = d.id
      JOIN harness_memory_observations o
        ON o.agent_id = s.agent_id AND o.validator_hotkey = s.validator_hotkey
       AND o.bench_version = s.bench_version AND o.outcome = 'scored'
     WHERE d.bench_version = 18 AND d.season_index BETWEEN :first AND :last;

Each run's composite is multiplied by its own factor (as the scorer applies
it), an agent's season score is the mean of its validator composites (the
ledger's quality composite averages the signed quorum composites), and agents
are re-ranked. This approximates the ranking only: it does not replay KOTH
dethrone z-bands or masked duel recomputation.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass

from ditto_screening_protocol.memory_factor import memory_factor_bps

MIB = 1 << 20


@dataclass(frozen=True)
class Run:
    composite: float
    p95: int
    peak: int
    limit: int


def factor(run: Run, free_mib: int, floor_bps: int) -> float:
    return memory_factor_bps(run.p95, free_mib * MIB, run.limit, floor_bps) / 10_000


def load(path: str) -> dict[int, dict[str, list[Run]]]:
    seasons: dict[int, dict[str, list[Run]]] = defaultdict(lambda: defaultdict(list))
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            seasons[int(row["season"])][row["agent"]].append(
                Run(
                    composite=float(row["composite"]),
                    p95=int(row["p95_bytes"]),
                    peak=int(row["peak_bytes"]),
                    limit=int(row["limit_bytes"]),
                )
            )
    return seasons


def ranked(scores: dict[str, float]) -> list[str]:
    return sorted(scores, key=lambda agent: (-scores[agent], agent))


def lead_lost_at_mib(lead: float, top: float, free_mib: int, floor_bps: int) -> float:
    """The P95 at which the leader's factor erases its lead over #2."""
    drop_needed = lead / top
    full_drop = (10_000 - floor_bps) / 10_000
    if drop_needed >= full_drop:
        return float("inf")
    return free_mib + drop_needed / full_drop * (2048 - free_mib)


def report(
    seasons: dict[int, dict[str, list[Run]]], free_mib: int, floor_bps: int
) -> None:
    print(f"\n== free <= {free_mib} MiB, x{floor_bps / 10_000:.2f} at the cap ==")
    moves = top_changes = 0
    for season in sorted(seasons):
        agents = seasons[season]
        base = {a: statistics.mean(r.composite for r in rs) for a, rs in agents.items()}
        adjusted = {
            a: statistics.mean(r.composite * factor(r, free_mib, floor_bps) for r in rs)
            for a, rs in agents.items()
        }
        before, after = ranked(base), ranked(adjusted)
        moved = [a for a in agents if before.index(a) != after.index(a)]
        moves += len(moved)
        top_changes += before[0] != after[0]
        lead = base[before[0]] - base[before[1]] if len(before) > 1 else float("nan")
        lost_at = lead_lost_at_mib(lead, base[before[0]], free_mib, floor_bps)
        print(
            f"season {season}: {len(agents):2d} agents, leader {before[0]}"
            f" -> {after[0]}, lead {lead:.4f}, leader loses the lead at P95"
            f" ~{lost_at:,.0f} MiB, rank moves {len(moved)}"
        )
        for agent in moved:
            print(
                f"    {agent}: #{before.index(agent) + 1} -> #{after.index(agent) + 1}"
            )
    print(f"total rank moves {moves}, leader changes {top_changes}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv")
    parser.add_argument(
        "--curve",
        action="append",
        default=[],
        metavar="FREE_MIB:FLOOR_BPS",
        help="curve to test; repeatable (default 1024:9000, 512:9000, 256:9000)",
    )
    args = parser.parse_args()
    seasons = load(args.csv)
    runs = [run for agents in seasons.values() for rs in agents.values() for run in rs]
    p95s = sorted(run.p95 / MIB for run in runs)
    print(
        f"{len(runs)} runs, {sum(len(a) for a in seasons.values())} agent-seasons;"
        f" P95 MiB min {p95s[0]:.0f}, median {statistics.median(p95s):.0f},"
        f" max {p95s[-1]:.0f}"
    )
    for curve in args.curve or ["1024:9000", "512:9000", "256:9000"]:
        free, floor = (int(part) for part in curve.split(":"))
        report(seasons, free, floor)


if __name__ == "__main__":
    main()
