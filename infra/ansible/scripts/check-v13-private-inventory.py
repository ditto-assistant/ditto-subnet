#!/usr/bin/env python3
"""Fail closed before stage convergence if the live GCP inventory is absent."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

INVENTORY = Path(__file__).parents[1] / "inventory" / "v13-private-gcp_compute.yml"
HOST = "ditto-v13-private-verifier-prod"
GROUP = "role_v13_private_verifier"


def validate_inventory(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("verifier inventory unavailable")
    group = data.get(GROUP)
    if not isinstance(group, dict) or group.get("hosts") != [HOST]:
        raise ValueError("exact verifier host group unavailable")
    meta = data.get("_meta")
    hostvars = meta.get("hostvars") if isinstance(meta, dict) else None
    host = hostvars.get(HOST) if isinstance(hostvars, dict) else None
    labels = host.get("labels") if isinstance(host, dict) else None
    if (
        not isinstance(labels, dict)
        or labels.get("role") != "v13_private_verifier"
        or labels.get("managed") != "terraform"
    ):
        raise ValueError("verifier host labels unavailable")


def main() -> int:
    # The google.cloud.gcp_compute plugin rejects arbitrary inventory names.
    if not INVENTORY.name.endswith("gcp_compute.yml"):
        print("verifier inventory filename unsupported", file=sys.stderr)
        return 1
    result = subprocess.run(
        ["ansible-inventory", "-i", str(INVENTORY), "--list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        print("verifier dynamic inventory parse failed", file=sys.stderr)
        return 1
    try:
        validate_inventory(json.loads(result.stdout))
    except (ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("exact private verifier host inventory verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
