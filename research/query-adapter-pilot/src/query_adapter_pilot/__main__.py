"""``python -m query_adapter_pilot`` and the ``query-adapter-pilot`` script."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import pilot


def main(argv: Sequence[str] | None = None) -> int:
    return pilot.main(argv)


if __name__ == "__main__":
    sys.exit(main())
