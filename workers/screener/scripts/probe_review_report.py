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
        reviewed = replay is not None
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
                "status": "reviewed" if reviewed else "battery_only",
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
            else ("no violation" if r["status"] == "reviewed" else "battery only")
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
    battery_hits = [r for r in table if r.get("battery_leads") and r["group"] != "pass"]
    lo = sum(_cost(t)[0] for t in tokens)
    hi = sum(_cost(t)[1] for t in tokens)
    return {
        "corpus": len(table),
        "prepared": sum(r["status"] != "not_run" for r in table),
        "reviewed": len(reviewed),
        "violations_reviewed": len(bad),
        "caught": sum(r["verified"] for r in bad),
        "caught_exact_mechanism": sum(
            r["verified"] and r["match"] == "exact" for r in bad
        ),
        "passes_reviewed": len(good),
        "passes_flagged": sum(r["verified"] for r in good),
        "battery_lead_on_violation": len(battery_hits),
        "median_tokens": statistics.median(tokens) if tokens else None,
        "median_review_min": round(statistics.median(review_s) / 60, 1)
        if review_s
        else None,
        "cost_usd_range_per_review": (
            round(lo / len(tokens), 2) if tokens else None,
            round(hi / len(tokens), 2) if tokens else None,
        ),
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
