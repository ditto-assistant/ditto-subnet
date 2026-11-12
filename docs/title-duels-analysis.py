"""Reproduce the numbers in ``docs/title-duels.md``.

Inputs are two read-only production exports (``gcloud-ditto-readonly``):

``conf.csv``  from ``confirmation_scores`` (bench >= 12): ``bench_version,
              agent_id, seed, validator_hotkey, composite, created_at,
              void_generation``;
``pins.csv``  from ``ledger_epoch_snapshots``: ``epoch_index, bench_version,
              champion_agent_id, incumbent_agent_id, pinned_at``.

Usage: ``python3 docs/title-duels-analysis.py <dir-with-csvs>``. Pure standard
library; the Student-t quantiles that make the frozen ``c(n)`` table are
computed here so the table can be re-derived without SciPy.
"""

from __future__ import annotations

import collections
import csv
import itertools
import math
import random
import statistics
import sys
from pathlib import Path

MARGIN = 0.007
N_MIN, N_MAX, K = 3, 15, 3
ALPHA_LOOK = 0.01
LEVEL = {13: 0.70, 14: 0.915, 15: 0.953}


def _betacf(a: float, b: float, x: float) -> float:
    tiny, c, d = 1e-300, 1.0, 1.0 - (a + b) * x / (a + 1.0)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        for aa in (
            m * (b - m) * x / ((a - 1 + 2 * m) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 1 + 2 * m)),
        ):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 3e-14:
            break
    return h


def _ibeta(a: float, b: float, x: float) -> float:
    if x <= 0.0 or x >= 1.0:
        return max(0.0, min(1.0, x))
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1 - x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def t_quantile(p: float, df: int) -> float:
    lo, hi = 0.0, 200.0
    for _ in range(200):
        mid = (lo + hi) / 2
        tail = 0.5 * _ibeta(df / 2, 0.5, df / (df + mid * mid))
        lo, hi = (mid, hi) if 1.0 - tail < p else (lo, mid)
    return (lo + hi) / 2


C = {n: t_quantile(1.0 - ALPHA_LOOK, n - 1) for n in range(N_MIN, N_MAX + 1)}


def decay(level: float) -> float:
    return math.exp(-2.0 * (min(max(level, 0.6), 1.0) - 0.6))


def mean_se(d: list[float]) -> tuple[float, float]:
    m = sum(d) / len(d)
    var = sum((x - m) ** 2 for x in d) / (len(d) - 1)
    return m, math.sqrt(var / len(d))


def today_beats(d: list[float], level: float) -> bool:
    m, se = mean_se(d)
    return m > max(MARGIN, min(1.64 * se, 2 * MARGIN)) * decay(level)


def duel_beats(d: list[float], level: float) -> bool:
    w = d[-N_MAX:]
    if len(w) < N_MIN:
        return False
    m, se = mean_se(w)
    return m > max(MARGIN * decay(level), C[len(w)] * se)


def load(root: Path) -> tuple[dict, list]:
    by: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    with (root / "conf.csv").open() as handle:
        for row in csv.reader(handle):
            by[(int(row[0]), row[1])][int(row[2])].append(float(row[4]))
    med = {k: {s: statistics.median(v) for s, v in d.items()} for k, d in by.items()}
    with (root / "pins.csv").open() as handle:
        pins = sorted(csv.reader(handle), key=lambda r: int(r[0]))
    changes, prev = [], None
    for _epoch, version, champion, _incumbent, at in pins:
        if prev and prev[0] == version and prev[1] != champion and champion:
            changes.append((at, int(version), prev[1], champion))
        prev = (version, champion)
    return med, changes


def residuals(med: dict, version: int) -> list[float]:
    deep = [k for k in med if k[0] == version and len(med[k]) >= 10]
    deep.sort(key=lambda k: -statistics.mean(med[k].values()))
    out: list[float] = []
    for a, b in itertools.combinations(deep[:30], 2):
        shared = sorted(set(med[a]) & set(med[b]))
        if len(shared) >= 10:
            d = [med[a][s] - med[b][s] for s in shared]
            m = statistics.mean(d)
            out += [x - m for x in d]
    return out


def main(root: Path) -> None:
    rng = random.Random(118)
    med, changes = load(root)
    print("c(n):", {n: round(c, 3) for n, c in C.items()})
    for version in (13, 14, 15):
        resid, level = residuals(med, version), LEVEL[version]
        walk = sum(
            any(
                today_beats(d[:n], level)
                for d in [[rng.choice(resid) for _ in range(10)]]
                for n in range(N_MIN, 11)
            )
            for _ in range(20000)
        )
        horizon = sum(
            any(
                duel_beats(d[:n], level)
                for d in [[rng.choice(resid) for _ in range(N_MAX)]]
                for n in range(N_MIN, N_MAX + 1)
            )
            for _ in range(20000)
        )
        cleared = total = 0
        for _at, v, old, new in changes:
            a, b = med.get((v, new), {}), med.get((v, old), {})
            shared = sorted(set(a) & set(b))[:N_MAX]
            if v != version or len(shared) < N_MIN:
                continue
            d = [a[s] - b[s] for s in shared]
            total += 1
            cleared += duel_beats(d, statistics.mean(b[s] for s in shared))
        print(
            f"v{version}: sd_d={statistics.pstdev(resid):.4f} "
            f"P(equal challenger wins a 10-seed walk today)={walk / 20000:.2f} "
            f"P(equal challenger wins a 15-pair duel window)={horizon / 20000:.3f} "
            f"crown changes clearing the duel test={cleared}/{total}"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
