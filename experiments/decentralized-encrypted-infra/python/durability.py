# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Availability, durability, sybil-withholding and sampling math for SN-473.

EXPERIMENT ONLY. Closed-form binomial models with independent nodes; real home
machines fail in correlated ways (power, ISP, one owner running many
identities), so treat every number as an upper bound on what independence buys.
Prints JSON to stdout.
"""

from __future__ import annotations

import json
import math

SCHEMES = {
    "replicate-3": (1, 3),
    "replicate-5": (1, 5),
    "rs-4-of-12": (4, 12),
    "rs-10-of-30": (10, 30),
    "rs-16-of-32": (16, 32),
    "rs-29-of-80": (29, 80),
}


def tail_at_least(n: int, k: int, p: float) -> float:
    """P[Binomial(n, p) >= k]."""
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def nines(p_fail: float) -> float:
    """-log10(p_fail), capped at 15: float64 cannot resolve 1 - p below ~1e-16."""
    return 15.0 if p_fail < 1e-15 else min(15.0, -math.log10(p_fail))


def availability() -> list[dict]:
    rows = []
    for p in (0.5, 0.7, 0.8, 0.9, 0.95, 0.99):
        row: dict = {"p_node_online": p}
        for name, (k, n) in SCHEMES.items():
            unavailable = 1 - tail_at_least(n, k, p)
            row[name] = {
                "p_unavailable": unavailable,
                "nines": round(nines(unavailable), 2),
            }
        rows.append(row)
    return rows


def durability() -> list[dict]:
    """Permanent loss before repair: each holder departs for good with
    probability q per day; repair restores the full set every `window` days."""
    rows = []
    for q_day in (0.001, 0.005, 0.02):
        for window in (1, 7):
            q = 1 - (1 - q_day) ** window
            row: dict = {"q_depart_per_day": q_day, "repair_window_days": window}
            for name, (k, n) in SCHEMES.items():
                p_loss = tail_at_least(n, n - k + 1, q)
                annual = 1 - (1 - p_loss) ** (365 / window)
                row[name] = {
                    "p_loss_per_year": annual,
                    "nines": round(nines(annual), 2),
                }
            rows.append(row)
    return rows


def repair_traffic() -> list[dict]:
    """Bytes the repairer reads per day, as a fraction of the stored object,
    when holders depart at q per day. Eager repair rebuilds each lost piece as
    soon as it is lost; lazy repair waits until `threshold` pieces remain and
    rebuilds them all from one k-piece read."""
    rows = []
    for q_day in (0.001, 0.005, 0.02):
        row: dict = {"q_depart_per_day": q_day}
        for name, (k, n) in SCHEMES.items():
            lost_per_day = n * q_day
            if k == 1:
                eager = lost_per_day  # copy one surviving replica per lost replica
                lazy = eager
            else:
                eager = lost_per_day  # read k pieces of size 1/k per rebuilt piece
                threshold = k + max(1, (n - k) // 3)
                lazy = lost_per_day / (n - threshold)  # one object read per batch
            row[name] = {"eager_read_fraction": eager, "lazy_read_fraction": lazy}
        rows.append(row)
    return rows


def sybil_withholding() -> list[dict]:
    """An adversary holding a fraction beta of randomly placed pieces can make
    an object unrecoverable by withholding n-k+1 of its n pieces."""
    rows = []
    for beta in (0.05, 0.1, 0.2, 1 / 3, 0.5):
        row: dict = {"adversary_fraction": round(beta, 4)}
        for name, (k, n) in SCHEMES.items():
            row[name] = tail_at_least(n, n - k + 1, beta)
        rows.append(row)
    return rows


def sampling() -> dict:
    """Random challenges needed to catch a holder missing a fraction f of its
    pieces with confidence 1-delta: s = ceil(ln(delta) / ln(1-f))."""
    table = []
    for f in (0.01, 0.05, 0.1, 0.5):
        row: dict = {"missing_fraction": f}
        for delta in (1e-2, 1e-6, 1e-9):
            row[f"delta_{delta:g}"] = math.ceil(math.log(delta) / math.log(1 - f))
        table.append(row)
    return {
        "por_challenges": table,
        "das_rate_half_note": (
            "With a rate-1/2 code an object stays recoverable unless more than "
            "half its chunks are withheld, so each uniform sample of a "
            "withheld object fails with probability >= 1/2 and s samples miss "
            "with probability <= 2^-s (s=20: < 1e-6)."
        ),
    }


def main() -> None:
    out = {
        "experiment": "sn473-durability-math",
        "model": "independent nodes, binomial tails; upper bounds under correlation",
        "schemes": {
            name: {"k": k, "n": n, "expansion": n / k}
            for name, (k, n) in SCHEMES.items()
        },
        "availability": availability(),
        "durability": durability(),
        "repair_traffic": repair_traffic(),
        "sybil_withholding": sybil_withholding(),
        "sampling": sampling(),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
