#!/usr/bin/env python3
"""Hash retained preparation logs/receipts and count cost/usage identifiers.

Inventory evidence, not a charge estimator. No content or raw IDs are emitted.
"""
import argparse
import json
from pathlib import Path
import re
from audit_openrouter_costs import digest, write_new

PREPARATION = ("dream", "settle", "seed_manifest", "final-labels", "final-force-graph")
IDENTIFIERS = re.compile(r"\bgen-[A-Za-z0-9_-]{5,180}\b")
COST_FIELDS = re.compile(r'"(?:total_cost|cost_credits|provider_cost_usd|cost_usd|usage|prompt_tokens|completion_tokens|input_tokens|output_tokens)"\s*:')


def inventory(root):
    rows = []
    # Explicit flat preparation directories; no dataset/credential/source scan.
    for directory in (root, root / "evidence"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix not in (".log", ".jsonl", ".json"):
                continue
            if not path.name.startswith(PREPARATION):
                continue
            identifiers, usage_fields, size = set(), 0, 0
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    size += len(line.encode("utf-8"))
                    identifiers.update(IDENTIFIERS.findall(line))
                    usage_fields += len(COST_FIELDS.findall(line))
            rows.append({"artifact": str(path.relative_to(root)), "sha256": digest(path),
                         "bytes": path.stat().st_size, "generation_id_count": len(identifiers),
                         "cost_or_usage_field_occurrences": usage_fields})
    if not rows:
        raise ValueError("no preparation evidence found")
    return {"schema": "preparation-cost-evidence-inventory-v1", "helper_sha256": digest(__file__),
            "files": rows, "file_count": len(rows),
            "files_with_generation_ids": sum(row["generation_id_count"] > 0 for row in rows),
            "files_with_cost_or_usage_fields": sum(row["cost_or_usage_field_occurrences"] > 0 for row in rows),
            "historical_preparation_cost_usd": None,
            "limitation": "Field/identifier inventory only, not proof of billed amount or completeness of all historical logs. Missing receipts are not zero. No generation IDs/content emitted; no provider or database calls."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inventory(args.root)
    write_new(args.output, result)
    print(json.dumps({k: result[k] for k in ("file_count", "files_with_generation_ids", "files_with_cost_or_usage_fields")}))


if __name__ == "__main__":
    main()
