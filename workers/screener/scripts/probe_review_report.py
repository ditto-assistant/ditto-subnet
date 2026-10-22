"""Aggregate probe-review shadow sessions against a labelled corpus.

    python scripts/probe_review_report.py --corpus corpus.json --sessions DIR

``corpus.json`` rows carry ``id8``, ``group`` (reject | pass | oracle_miss),
``name``, ``version``, ``lang``, ``invariants`` (production ground truth), and
``baseline_wall_s``. Each session directory holds ``prepare.json`` and, when the
reviewer ran, ``replay.json`` plus ``review_meta.json`` (reviewer usage and the
operator's mechanism-match label). Output is a Markdown table and summary.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

# Claude Sonnet list price per million tokens; subagent token counts are not
# split into input/output, so cost is bracketed between all-input and a 90/10
# input/output mix.
_IN, _OUT = 3.0, 15.0


def _cost(tokens: int) -> tuple[float, float]:
    return tokens * _IN / 1e6, tokens * (0.9 * _IN + 0.1 * _OUT) / 1e6


def _load(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def rows(corpus: list[dict[str, Any]], sessions: Path) -> list[dict[str, Any]]:
    out = []
    for item in corpus:
        root = sessions / item["id8"]
        prepare = _load(root / "prepare.json")
        replay = _load(root / "replay.json")
        meta = _load(root / "review_meta.json") or {}
        if prepare is None:
            out.append({**item, "status": "not_run"})
            continue
        battery = prepare["battery"]
        leads = [lead["item_id"] for lead in battery["leads"]]
        # A replay that failed validation or citation checks is inconclusive,
        # not a completed review that found nothing.
        replay_failed = bool(replay and replay.get("problems"))
        reviewed = replay is not None and not replay_failed
        verified = bool(replay and replay.get("verified"))
        found = sorted(
            {
                f["invariant"]
                for f in (replay or {}).get("findings", [])
                if f.get("verified")
            }
        )
        out.append(
            {
                **item,
                "status": "reviewed"
                if reviewed
                else "replay_failed"
                if replay_failed
                else "battery_only",
                "battery_leads": leads,
                "battery_s": prepare["timings"].get("battery_s"),
                "build_s": prepare["timings"].get("build_s"),
                "verified": verified,
                "found": found,
                "match": meta.get("mechanism_match", ""),
                "tokens": meta.get("subagent_tokens"),
                "review_s": (meta.get("duration_ms") or 0) / 1000 or None,
                "probes": (meta.get("final") or {}).get("probes"),
                "policy_sensitive": meta.get("policy_sensitive", False),
                "assessment": meta.get("operator_assessment", ""),
                "note": meta.get("note", ""),
            }
        )
    return out


def render(table: list[dict[str, Any]]) -> str:
    lines = [
        "| Agent | Group | Lang | Production | Battery leads | Shadow verdict "
        "| Mechanism | Tokens | Review min | Baseline min |",
        "|---|---|---|---|---|---|---|---:|---:|---:|",
    ]
    for r in sorted(table, key=lambda r: (r["group"], r.get("status", ""), r["name"])):
        if r["status"] == "not_run":
            continue
        verdict = (
            "violation " + "/".join(r["found"])
            if r["verified"]
            else {
                "reviewed": "no violation",
                "replay_failed": "replay failed",
            }.get(r["status"], "battery only")
        )
        baseline = (
            round(r["baseline_wall_s"] / 60, 1) if r.get("baseline_wall_s") else ""
        )
        production = "/".join(r["invariants"]) or "pass"
        leads = ", ".join(r["battery_leads"]) or "-"
        minutes = round(r["review_s"] / 60, 1) if r["review_s"] else ""
        cells = [
            f"{r['name']} v{r['version']} `{r['id8']}`",
            r["group"],
            r["lang"],
            production,
            leads,
            verdict,
            r["match"] or "",
            str(r["tokens"] or ""),
            str(minutes),
            str(baseline),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def summary(table: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [r for r in table if r["status"] == "reviewed"]
    bad = [r for r in reviewed if r["group"] in {"reject", "oracle_miss"}]
    good = [r for r in reviewed if r["group"] == "pass"]
    tokens = [r["tokens"] for r in reviewed if r["tokens"]]
    review_s = [r["review_s"] for r in reviewed if r["review_s"]]
    doubtful = {"likely_artifact", "likely_false_finding"}
    strict = [
        r
        for r in bad
        if r["verified"]
        and r["assessment"] not in doubtful
        and not r["policy_sensitive"]
    ]
    by_invariant: dict[str, dict[str, int]] = {}
    for r in bad:
        for inv in r["invariants"]:
            row = by_invariant.setdefault(inv, {"n": 0, "caught": 0, "same_inv": 0})
            row["n"] += 1
            row["caught"] += r["verified"]
            row["same_inv"] += r["verified"] and inv in r["found"]
    lo = sum(_cost(t)[0] for t in tokens)
    hi = sum(_cost(t)[1] for t in tokens)
    flagged = [r for r in good if r["verified"]]
    return {
        "corpus": len(table),
        "prepared": sum(r["status"] != "not_run" for r in table),
        "reviewed": len(reviewed),
        "replay_failed": sum(r["status"] == "replay_failed" for r in table),
        "violations_reviewed": len(bad),
        "caught_verified": sum(r["verified"] for r in bad),
        "caught_strict": len(strict),
        "caught_exact_mechanism": sum(
            r["verified"] and r["match"] == "exact" for r in bad
        ),
        "caught_same_class": sum(
            r["verified"] and r["match"] == "same_class" for r in bad
        ),
        "caught_different_mechanism": sum(
            r["verified"] and r["match"] == "different" for r in bad
        ),
        "missed": sum(not r["verified"] for r in bad),
        "by_production_invariant": dict(sorted(by_invariant.items())),
        "battery_lead_on_reviewed_violation": sum(
            bool(r["battery_leads"]) for r in bad
        ),
        "passes_reviewed": len(good),
        "passes_flagged": len(flagged),
        "passes_flagged_by_assessment": {
            a: sum((r["assessment"] or "unassessed") == a for r in flagged)
            for a in sorted({r["assessment"] or "unassessed" for r in flagged})
        },
        "median_tokens": statistics.median(tokens) if tokens else None,
        "median_review_min": round(statistics.median(review_s) / 60, 1)
        if review_s
        else None,
        "p90_review_min": round(sorted(review_s)[(9 * len(review_s) - 1) // 10] / 60, 1)
        if review_s
        else None,
        "cost_usd_range_per_review": (
            round(lo / len(tokens), 2) if tokens else None,
            round(hi / len(tokens), 2) if tokens else None,
        ),
        "baseline_median_min_reviewed_rejects": round(
            statistics.median(
                [r["baseline_wall_s"] for r in bad if r.get("baseline_wall_s")]
            )
            / 60,
            1,
        )
        if any(r.get("baseline_wall_s") for r in bad)
        else None,
        "baseline_median_min_reviewed_passes": round(
            statistics.median(
                [r["baseline_wall_s"] for r in good if r.get("baseline_wall_s")]
            )
            / 60,
            1,
        )
        if any(r.get("baseline_wall_s") for r in good)
        else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    table = rows(json.loads(Path(args.corpus).read_text()), Path(args.sessions))
    if args.json:
        print(json.dumps({"summary": summary(table), "rows": table}, indent=1))
        return
    print(render(table))
    print()
    print(json.dumps(summary(table), indent=1))


if __name__ == "__main__":
    main()
