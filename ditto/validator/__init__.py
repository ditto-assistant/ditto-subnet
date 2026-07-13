"""Validator worker (WIP).

A standalone, HTTP-decoupled validator daemon co-located with the platform on
the SN118 app VM (run as a separate systemd/pm2 process — never inside the API
process). Each sweep it pulls agents awaiting evaluation from the platform's
``/validator/*`` API, scores each through the hosted ``dittobench-api`` (by
presigned tarball URL, run_size=full), reports the signed
score back, and sets chain weights via Pylon identity ``put_weights``.

The worker talks to the platform only over HTTP + the chain — no direct DB —
so this exact process is what an independent third-party validator would run.

Entry point: ``python -m ditto.validator`` (see :mod:`ditto.validator.__main__`).
"""

from __future__ import annotations

from ditto.validator.config import ValidatorConfig, parse_validator_config_from_env
from ditto.validator.worker import ValidatorWorker

__all__ = [
    "ValidatorConfig",
    "ValidatorWorker",
    "parse_validator_config_from_env",
]
