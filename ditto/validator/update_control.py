"""Local, non-secret coordination state for safe validator image updates."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Literal

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# This is deliberately independent of the heartbeat and benchmark protocols.
# Increment it for any release that cannot safely replace a running validator
# from the previous epoch (wire/config/consensus/sidecar boundary changes).
VALIDATOR_COMPATIBILITY_EPOCH = 1
VALIDATOR_UPDATE_PROTOCOL = 1
VALIDATOR_UPDATE_STATE_PATH = Path("/tmp/ditto-validator-update-state.json")
VALIDATOR_BOOTSTRAP_RESUMED_PATH = Path("/app/.ditto-validator-bootstrap-resumed")

ValidatorUpdateState = Literal["starting", "ready", "working", "drained", "stopping"]


def bootstrap_should_start_drained(
    enabled: bool,
    *,
    marker_path: Path = VALIDATOR_BOOTSTRAP_RESUMED_PATH,
) -> bool:
    """Apply updater bootstrap only once for a container's writable layer."""
    return enabled and not marker_path.exists()


def mark_bootstrap_resumed(
    *, marker_path: Path = VALIDATOR_BOOTSTRAP_RESUMED_PATH
) -> bool:
    """Persist USR2 before allowing work so a later restart cannot re-drain."""
    try:
        marker_path.write_text("resumed\n")
        marker_path.chmod(0o600)
        return True
    except OSError as error:
        logger.error("could not persist validator bootstrap resume: %s", error)
        return False


def write_update_state(
    state: ValidatorUpdateState,
    *,
    platform_accepted: bool = False,
    path: Path = VALIDATOR_UPDATE_STATE_PATH,
) -> None:
    """Atomically publish the worker's local update state.

    The updater reads this file with ``docker exec``. It contains no host,
    wallet, agent, run, score, or secret data. Failure to write is fail-safe for
    updates: the worker continues, while the updater never sees ``drained``.
    """
    payload = {
        "compatibility_epoch": VALIDATOR_COMPATIBILITY_EPOCH,
        "heartbeat_protocol": HEARTBEAT_PROTOCOL_VERSION,
        "pid": os.getpid(),
        "platform_accepted": platform_accepted,
        "state": state,
        "update_protocol": VALIDATOR_UPDATE_PROTOCOL,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as error:
        logger.warning("could not publish validator update state: %s", error)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
