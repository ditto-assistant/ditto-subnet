"""Report-only probe battery run by the screening gate after its decision.

The gate launches ``python -m ditto_screener.probe_review gate-battery`` as a
killable subprocess against the image it already built and screened. The
command prints one bounded JSON summary: lead names only, never probe
transcripts, prompts, or harness output, because the summary is telemetry that
leaves the screener node. Nothing here can change a screening outcome.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ditto_screener.probe_review import battery
from ditto_screener.probe_review.sandbox import Session

MAX_LEADS = 32
_ITEM_ID = re.compile(r"^[a-z0-9_]{1,64}$")
_INVARIANT = re.compile(r"^I[1-8]$")
_ENCODING = re.compile(r"^[a-z_]{1,32}$")


def container_names(token: str) -> tuple[str, str, str]:
    """(network, gateway, harness) names a session with ``token`` creates."""
    probe = Session(root="", image="", artifact_sha256="", bench_version=0, token=token)
    return probe.network, probe.gateway, probe.harness


def summarize(
    report: dict[str, Any], *, probe_count: int, elapsed_s: float
) -> dict[str, Any]:
    """Bound a battery report to the wire shape Platform stores."""
    leads = []
    for lead in report.get("leads") or []:
        item_id = str(lead.get("item_id") or "")
        invariant = str(lead.get("invariant") or "").split("/")[0]
        if _ITEM_ID.match(item_id) and _INVARIANT.match(invariant):
            leads.append({"item_id": item_id, "invariant": invariant})
    encoding = report.get("format")
    valid_encoding = isinstance(encoding, str) and bool(_ENCODING.match(encoding))
    return {
        "status": "complete" if encoding else "calibration_failed",
        "encoding": encoding if valid_encoding else None,
        "seed_first": bool(report.get("seed_first")),
        "items_run": min(len(report.get("items") or []), 128),
        "probe_count": min(max(probe_count, 0), 512),
        "leads": leads[:MAX_LEADS],
        "battery_ms": min(int(elapsed_s * 1000), 3_600_000),
    }


def run_gate_battery(
    *, image: str, root: str, artifact_sha256: str, bench_version: int, token: str
) -> dict[str, Any]:
    """Start a probe sandbox on ``image``, run the battery, always stop it."""
    session = Session(
        root=root,
        image=image,
        artifact_sha256=artifact_sha256,
        bench_version=bench_version,
        token=token,
    )
    started = time.monotonic()
    try:
        session.start()
        report = battery.run_battery(session)
    finally:
        session.stop()
    return summarize(
        report, probe_count=session.probe_count, elapsed_s=time.monotonic() - started
    )
