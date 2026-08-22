"""Trusted entrypoint for the non-adversarial public-practice test runner."""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from collections.abc import Sequence
from pathlib import Path

COMPLETION_MARKER = b"dittobench-practice-tests-complete-v1\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dittobench-coding-practice-grader")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--completion-fd", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = args.workspace.resolve(strict=True)
    tests = workspace / "tests"
    if not tests.is_dir() or tests.is_symlink():
        return 2

    # `-I` starts this process without the candidate cwd or PYTHONPATH. Import
    # unittest above, then append candidate paths so standard-library modules
    # always win over files such as `unittest.py` in the workspace.
    for path in (workspace, tests):
        rendered = str(path)
        if rendered not in sys.path:
            sys.path.append(rendered)

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(tests),
        pattern=args.pattern,
        top_level_dir=str(tests),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    os.write(args.completion_fd, COMPLETION_MARKER)
    os.close(args.completion_fd)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
