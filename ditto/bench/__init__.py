"""DittoBench: the public benchmark suite for the Ditto Bittensor subnet.

Miners submit a Go harness packaged as an OCI image; validators run each
harness in an isolated Docker sandbox over the public fixture set, score the
responses, and set per-mechanism weights on chain.

Submodules:
- :mod:`ditto.bench.loader`: dataclasses + JSONL loaders for fixtures.
- :mod:`ditto.bench.runner`: contributor-facing validator-runner stub that
  drives a harness image over stdio and emits per-case scores.

Fixtures live under :mod:`ditto.bench.fixtures` and JSON schemas under
:mod:`ditto.bench.schemas`. The protocol and scoring contracts live as
markdown under :mod:`ditto.bench.docs`.
"""

from __future__ import annotations

SCHEMA_VERSION = "dittobench/1"
"""Wire-schema version every harness and validator must echo.

Increment on any backward-incompatible change to
``schemas/challenge_request.schema.json``, ``miner_response.schema.json``,
or ``score.schema.json``. Mirrors ``bittensor.SchemaVersion`` in the Go
source of truth.
"""
