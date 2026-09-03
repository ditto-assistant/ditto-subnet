#!/usr/bin/env python3
"""Exec the fixed scorer binary only after deriving authority from attestations."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def verifier():
    path = Path(__file__).with_name("verify-scorer-service.py")
    specification = importlib.util.spec_from_file_location(
        "scorer_service_verify", path
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("scorer service verifier cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


VERIFY = verifier()


def main() -> int:
    repository = VERIFY.verify_service()
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    environment = dict(os.environ)
    environment["DITTOBENCH_CODING_RUNTIME_IMAGE_REPOSITORY"] = repository
    environment["DITTOBENCH_CODING_EXECUTOR_CONTROL_TOKEN_FILE"] = str(
        credentials / "control-token"
    )
    os.execve(str(VERIFY.RUNTIME_BINARY), [str(VERIFY.RUNTIME_BINARY)], environment)
    return 111


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VERIFY.VerificationError:
        print("attested scorer service refused runtime", file=sys.stderr)
        raise SystemExit(1) from None
