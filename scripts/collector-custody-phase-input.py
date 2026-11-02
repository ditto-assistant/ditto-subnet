#!/usr/bin/env python3
"""Typed public lifecycle input; never credentials or Terraform invocation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def write_phase(phase: str, rpc: str, destination: Path) -> None:
    if phase not in ("bootstrap", "armed", "locked", "sealed"):
        raise ValueError("unknown custody phase")
    if rpc not in ("true", "false") or (rpc == "true" and phase != "sealed"):
        raise ValueError("RPC requires explicit sealed phase")
    fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as handle:
        json.dump(
            {
                "collector_custody_phases": {
                    "registration": phase,
                    "transfer": phase,
                },
                "collector_runtime_rpc_egress": rpc == "true",
            },
            handle,
        )
        handle.write("\n")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("Expected phase, explicit RPC boolean and output path.")
    write_phase(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
