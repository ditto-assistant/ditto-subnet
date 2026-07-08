#!/usr/bin/env python3
"""Validator auto-updater wrapper — the easy pm2 production entrypoint.

Inspired by RESI Labs' validator setup
(https://github.com/resi-labs-ai/RESI-models, docs/VALIDATOR.md), where the
validator runs under pm2 through a single Python auto-updater that periodically
checks git for updates and (re)launches the actual validator process.

    # one-liner: start the auto-updater under pm2 (see `make prod-up`)
    pm2 start "uv run python scripts/start_validator.py" --name ditto_autoupdater

    pm2 logs ditto_autoupdater   # tail logs
    pm2 restart ditto_autoupdater
    pm2 stop ditto_autoupdater

What this does, on a loop:
  1. load ./.env into the environment the child inherits,
  2. launch the validator daemon as a managed subprocess,
  3. every UPDATE_INTERVAL seconds, if the tracked git branch has moved,
     `git pull` + `uv sync` and restart the child,
  4. if the child exits on its own, restart it.

Prerequisites (not provisioned by this script — keep it honest):
  * pm2 installed (``npm i -g pm2``) and used to launch this wrapper.
  * A filled-in ``.env`` (``cp .env.example .env``). The validator needs the
    ``VALIDATOR_* / PYLON_* / NETUID / SUBTENSOR_NETWORK`` settings; see
    ``ditto/validator/config.py``.
  * Pylon reachable at ``PYLON_URL``. Pylon is run separately (there is no
    docker-compose in this repo); generate its token with
    ``openssl rand -base64 32``.

STATUS: WIP scaffold. The update/restart mechanics are deliberately minimal and
need hardening (health checks, backoff, update cadence) before release — see the
TODO below.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

REPO_ROOT = Path(__file__).resolve().parent.parent

# The managed process is the REAL validator daemon, which already exists in this
# repo: see README "Validator worker" and ditto/validator/__main__.py. It runs
# the full queue -> score -> set-weights loop and drains on SIGTERM/SIGINT.
#
# NOTE: an earlier scaffold brief referenced ``python -m ditto.api_server`` as a
# placeholder, but that module lives in the sibling ``ditto-platform`` repo, not
# here — launching it would just crash. ``ditto.validator`` is the correct,
# existing entrypoint, so we launch it directly.
#
# TODO(release): harden this auto-updater (subprocess health checks, restart
# backoff, and a confirmed update cadence) before it becomes the canonical
# production entrypoint.
VALIDATOR_CMD = ["uv", "run", "python", "-m", "ditto.validator"]

logger = logging.getLogger("ditto.autoupdater")

_stopping = False


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (mirrors ``set -a && . ./.env && set +a``).

    Existing environment variables win, so pm2 ``--update-env`` overrides still
    take effect. We avoid a hard dependency on python-dotenv to keep the wrapper
    self-contained.
    """
    if not path.exists():
        logger.warning(".env not found at %s — relying on the ambient env", path)
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True).strip()


def _remote_has_update(branch: str) -> bool:
    """True if ``origin/<branch>`` has commits our checkout does not."""
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", branch],
            cwd=REPO_ROOT,
            check=True,
        )
        local = _git("rev-parse", "HEAD")
        remote = _git("rev-parse", f"origin/{branch}")
    except subprocess.CalledProcessError as exc:
        logger.warning("git update check failed (skipping): %s", exc)
        return False
    return local != remote


def _apply_update(branch: str) -> None:
    logger.info("update detected on origin/%s — pulling + syncing", branch)
    subprocess.run(
        ["git", "pull", "--ff-only", "origin", branch], cwd=REPO_ROOT, check=True
    )
    subprocess.run(["uv", "sync"], cwd=REPO_ROOT, check=True)


def _launch() -> subprocess.Popen[bytes]:
    logger.info("launching validator: %s", " ".join(VALIDATOR_CMD))
    return subprocess.Popen(VALIDATOR_CMD, cwd=REPO_ROOT)


def _terminate(proc: subprocess.Popen[bytes], grace: float = 35.0) -> None:
    """Ask the validator to drain (SIGTERM), then SIGKILL if it overruns."""
    if proc.poll() is not None:
        return
    logger.info("stopping validator (pid=%s)", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.warning("validator did not drain in %.0fs — killing", grace)
        proc.kill()
        proc.wait()


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _stopping
    logger.info("received signal %s — shutting down", signum)
    _stopping = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("VALIDATOR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _load_dotenv(REPO_ROOT / ".env")

    auto_update = os.environ.get("AUTO_UPDATE", "1") not in ("0", "false", "False")
    branch = os.environ.get("AUTO_UPDATE_BRANCH", "main")
    interval = int(os.environ.get("UPDATE_INTERVAL", "300"))  # ~5 min, RESI-style

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    logger.info(
        "auto-updater started (auto_update=%s branch=%s interval=%ss)",
        auto_update,
        branch,
        interval,
    )

    proc = _launch()
    try:
        while not _stopping:
            # Sleep in short slices so signals are handled promptly.
            waited = 0
            while waited < interval and not _stopping:
                time.sleep(min(5, interval - waited))
                waited += 5
                if proc.poll() is not None:
                    logger.warning(
                        "validator exited (code=%s) — restarting", proc.returncode
                    )
                    proc = _launch()
            if _stopping:
                break
            if auto_update and _remote_has_update(branch):
                _apply_update(branch)
                _terminate(proc)
                proc = _launch()
    finally:
        _terminate(proc)
    logger.info("auto-updater stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
