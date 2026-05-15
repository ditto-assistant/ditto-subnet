"""Module entry point so ``python -m ditto.bench.runner`` invokes the CLI."""

from __future__ import annotations

import sys

from ditto.bench.runner.run import main

if __name__ == "__main__":
    sys.exit(main())
