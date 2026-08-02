"""Git revisions: the one this process was built from, and the one on disk.

Two different facts that are easy to confuse and were confused during the
2026-07-25 near-outage:

* the revision the **running process** started from -- resolved once at boot
  and carried on :data:`app.state.commit_hash`; and
* the revision **currently checked out** in the deploy directory, which a
  deploy changes without the running process noticing.

A deploy that checks out new code and then fails before restarting the app
leaves those two disagreeing while every git-layer signal reports success.
:func:`checked_out_commit` re-reads the second one so ``/health`` can report
both and say plainly whether they match.

The re-read is a subprocess, so it is cached for
:data:`CHECKED_OUT_TTL_SECONDS` and single-flighted: ``/health`` is a
Prometheus-scraped liveness probe and must not fork ``git`` per request.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time

UNKNOWN = "unknown"
_BUILD_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

CHECKED_OUT_TTL_SECONDS = 15.0
"""How long a resolved checkout revision is reused.

Long enough that scrape traffic cannot fork ``git`` in a loop, short enough
that an operator watching ``/health`` through a deploy sees drift appear and
clear within one refresh.
"""

# A plain threading lock, not asyncio's: the refresh runs in a worker thread,
# and a module-level asyncio.Lock binds itself to the first event loop that
# awaits it, which breaks the moment a second loop (another test, another
# uvicorn run) touches it.
_lock = threading.Lock()
_cached: tuple[float, str] | None = None


def resolve_commit_hash() -> str:
    """Return the git revision of the checkout, or ``"unknown"``.

    Falls back to ``"unknown"`` on any failure (subprocess error, non-zero
    exit, missing git binary, no ``.git`` directory). The fallback lets
    deploy artifacts without git history boot cleanly.
    """
    baked = os.environ.get("DITTO_BUILD_COMMIT", "").strip().lower()
    if baked:
        return baked if _BUILD_COMMIT_RE.fullmatch(baked) else UNKNOWN
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return UNKNOWN
    if result.returncode != 0:
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN


def _refresh_checked_out_commit() -> str:
    """Re-read the checkout under the lock, so concurrent probes fork one git."""
    global _cached
    with _lock:
        cached = _cached
        if (
            cached is not None
            and time.monotonic() - cached[0] < CHECKED_OUT_TTL_SECONDS
        ):
            return cached[1]
        value = resolve_commit_hash()
        _cached = (time.monotonic(), value)
        return value


async def checked_out_commit() -> str:
    """Return the revision on disk now, cached for a few seconds.

    Never raises: an unresolvable checkout reports ``"unknown"``, which
    callers must treat as "cannot tell" rather than as drift. The subprocess
    runs off the event loop so a slow ``git`` cannot stall request handling.
    """
    cached = _cached
    if cached is not None and time.monotonic() - cached[0] < CHECKED_OUT_TTL_SECONDS:
        return cached[1]
    return await asyncio.to_thread(_refresh_checked_out_commit)


def reset_cache() -> None:
    """Drop the cached checkout revision. For tests."""
    global _cached
    _cached = None


def commits_diverged(running: str, checked_out: str) -> bool:
    """Whether the process is provably serving something other than the checkout.

    Only ``True`` when both revisions are known. A missing revision is an
    absence of evidence, and reporting drift from it would fire on every host
    that deploys without git history.
    """
    if not running or not checked_out:
        return False
    if running == UNKNOWN or checked_out == UNKNOWN:
        return False
    return running != checked_out
