#!/usr/bin/env python3
"""Attach exact private Sol handoffs to a frozen L4 replay manifest offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_single_sol_calibration import _write_private_json

IDENTITY_FIELDS = (
    "agent_id",
    "attempt_id",
    "artifact_sha256",
    "policy_version",
    "manifest_digest",
    "review_settings_revision",
)


def _private_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("private manifest must be a non-symlink mode-0600 file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("private manifest must be a JSON object")
    return value


def _attach(
    l4_manifest: dict[str, object], handoff_manifest: dict[str, object]
) -> dict[str, object]:
    if handoff_manifest.get("schema_version") != 1:
        raise ValueError("unsupported Sol handoff schema")
    if l4_manifest.get("policy_version") != handoff_manifest.get("policy_version"):
        raise ValueError("L4 and Sol policy versions differ")
    raw_cases = l4_manifest.get("cases")
    raw_handoffs = handoff_manifest.get("items")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("L4 manifest needs cases")
    if not isinstance(raw_handoffs, list) or not raw_handoffs:
        raise ValueError("Sol handoff manifest needs items")
    handoffs: dict[tuple[object, object], dict[str, object]] = {}
    for raw in raw_handoffs:
        if not isinstance(raw, dict):
            raise ValueError("Sol handoff item must be an object")
        key = (raw.get("agent_id"), raw.get("attempt_id"))
        if key in handoffs:
            raise ValueError("duplicate exact Sol handoff identity")
        handoffs[key] = raw
    cases: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("L4 case must be an object")
        key = (raw.get("agent_id"), raw.get("attempt_id"))
        if key in seen:
            raise ValueError("duplicate exact L4 case identity")
        seen.add(key)
        handoff = handoffs.get(key)
        if handoff is None:
            raise ValueError("L4 case lacks an exact Sol handoff")
        if raw.get("sol_investigator") is not None:
            raise ValueError("L4 case already has a Sol investigator handoff")
        for field in IDENTITY_FIELDS:
            if raw.get(field) != handoff.get(field):
                raise ValueError(f"L4 and Sol {field} differ")
        investigator = handoff.get("sol_investigator")
        if not isinstance(investigator, dict):
            raise ValueError("Sol investigator handoff must be an object")
        for field in IDENTITY_FIELDS:
            if investigator.get(field) != raw.get(field):
                raise ValueError(f"nested Sol {field} differs from exact L4 case")
        cases.append({**raw, "sol_investigator": investigator})
    if set(handoffs) != seen:
        raise ValueError("Sol handoff has an unmatched exact attempt")
    return {**l4_manifest, "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l4-manifest", type=Path, required=True)
    parser.add_argument("--handoff-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {
        args.l4_manifest.resolve(),
        args.handoff_file.resolve(),
    }:
        raise SystemExit("output must differ from both private inputs")
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("output already exists")
    result = _attach(_private_json(args.l4_manifest), _private_json(args.handoff_file))
    _write_private_json(args.output, result)
    print(json.dumps({"attached_cases": len(result["cases"])}, sort_keys=True))


if __name__ == "__main__":
    main()
