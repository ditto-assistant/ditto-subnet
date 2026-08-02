"""Local-dev scenario-injection simulator.

Seeds the local Postgres with named, deterministic scenarios so the public
dashboard (``dashboard/index.html`` served at ``http://localhost:8000/``) can
be exercised without any chain, Pylon, or mainnet dependency.

**Local-dev tooling only.** Nothing in :mod:`ditto.simulator` is imported by
production code paths, and it must never be: it fabricates rows (hotkeys,
digests, signatures) that satisfy DB constraints but are not chain-verifiable.

Usage::

    uv run python -m ditto.simulator --list
    uv run python -m ditto.simulator <scenario> [--seed N] [--no-wipe]
"""
