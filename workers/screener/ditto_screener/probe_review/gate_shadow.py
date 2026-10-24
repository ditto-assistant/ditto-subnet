"""Report-only probe battery and reviewer run by the gate after its decision.

The gate launches ``python -m ditto_screener.probe_review gate-battery`` as a
killable subprocess against the image it already built and screened. The
command prints one bounded JSON line for the battery and, when the reviewer is
enabled, a second for the review: lead names, invariants, citations and usage
only, never probe transcripts, prompts, claims or harness output, because the
summary is telemetry that leaves the screener node. Printing the battery first
keeps it when the review is killed at the lease budget. Nothing here can
change a screening outcome.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ditto_screener.probe_review import agent_loop, battery, tools
from ditto_screener.probe_review.sandbox import Session

MAX_LEADS = 32
_ITEM_ID = re.compile(r"^[a-z0-9_]{1,64}$")
_INVARIANT = re.compile(r"^I[1-8]$")
_ENCODING = re.compile(r"^[a-z_]{1,32}$")
_CITATION = re.compile(r"^[\w./+-]{1,160}:\d{1,7}$")
MAX_REVIEW_FINDINGS = 4
MAX_FINDING_CITATIONS = 4
# Wall time kept back for the verdict replay and session stop after the loop.
# The settings floor (probe_review_timeout_seconds >= 120) leaves the loop at
# least _MIN_REVIEW_S whenever the lease is not the tighter bound.
_REVIEW_TAIL_S = 45.0
_MIN_REVIEW_S = 60.0


@dataclass(frozen=True)
class ReviewSpec:
    """Reviewer agent limits and the node-local key the gate configured."""

    key_file: str
    base_url: str
    model: str
    max_cost_usd: float
    timeout_seconds: float


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
        "status": "complete" if valid_encoding else "calibration_failed",
        "encoding": encoding if valid_encoding else None,
        "seed_first": bool(report.get("seed_first")),
        "items_run": min(len(report.get("items") or []), 128),
        "probe_count": min(max(probe_count, 0), 512),
        "leads": leads[:MAX_LEADS],
        "battery_ms": min(int(elapsed_s * 1000), 3_600_000),
    }


def summarize_review(result: dict[str, Any] | None, *, status: str) -> dict[str, Any]:
    """Bound one reviewer result to the wire shape Platform stores."""
    result = result or {}
    replay = result.get("replay") if isinstance(result.get("replay"), dict) else None
    findings = []
    for finding in (replay or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        invariant = str(finding.get("invariant") or "").split("/")[0]
        if not _INVARIANT.match(invariant):
            continue
        citations = [
            c
            for c in finding.get("source_citations") or []
            if isinstance(c, str) and _CITATION.match(c)
        ][:MAX_FINDING_CITATIONS]
        findings.append(
            {
                "invariant": invariant,
                "verified": finding.get("verified") is True,
                "citations": citations,
            }
        )
    verified = bool(replay and replay.get("verified") is True)
    return {
        "status": status,
        "verified": verified,
        "invariants": sorted({f["invariant"] for f in findings if f["verified"]}),
        "findings": findings[:MAX_REVIEW_FINDINGS],
        "requests": min(int(result.get("requests") or 0), 1000),
        "prompt_tokens": min(int(result.get("prompt_tokens") or 0), 50_000_000),
        "completion_tokens": min(int(result.get("completion_tokens") or 0), 5_000_000),
        "cost_usd": min(max(float(result.get("cost_usd") or 0.0), 0.0), 50.0),
        "review_ms": min(int(float(result.get("wall_s") or 0) * 1000), 3_600_000),
    }


def _review_status(result: dict[str, Any]) -> str:
    if result.get("untracked_spend"):
        return "untracked_spend"
    if result.get("complete"):
        return "complete"
    if result.get("budget_exhausted"):
        return "budget_exhausted"
    return "no_verdict"


def _prepare(
    session: Session, archive: Path, report: dict[str, Any], artifact_sha256: str
) -> None:
    """Write the prepare.json the reviewer reads: source, triage, battery."""
    files = tools.extract_source(archive, Path(session.root) / "src")
    Path(session.root, "prepare.json").write_text(
        json.dumps(
            {
                "artifact_sha256": artifact_sha256,
                "image": session.image,
                "source_files": files,
                "timings": {},
                "triage": tools.triage(archive),
                "battery": report,
            }
        )
    )


def run_gate_battery(
    *,
    image: str,
    root: str,
    artifact_sha256: str,
    bench_version: int,
    token: str,
    emit: Callable[[dict[str, Any]], None],
    archive: str | None = None,
    review: ReviewSpec | None = None,
    deadline_seconds: float | None = None,
) -> None:
    """Start a probe sandbox on ``image``, run the battery, always stop it.

    Emits the battery summary first and, with ``review``, the reviewer summary
    second; the reviewer only gets the time left before ``deadline_seconds``.
    """
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
        emit(
            summarize(
                report,
                probe_count=session.probe_count,
                elapsed_s=time.monotonic() - started,
            )
        )
        if review is None or archive is None:
            return
        if not report.get("format"):
            # Uncalibrated harnesses give the reviewer nothing to script.
            emit({"review": summarize_review(None, status="skipped")})
            return
        left = review.timeout_seconds
        if deadline_seconds is not None:
            left = min(left, deadline_seconds - (time.monotonic() - started))
        left -= _REVIEW_TAIL_S
        if left < _MIN_REVIEW_S:
            emit({"review": summarize_review(None, status="skipped")})
            return
        try:
            _prepare(session, Path(archive), report, artifact_sha256)
            session.restart_harness()
            ledger = agent_loop.Ledger(max_cost_usd=review.max_cost_usd)
            with agent_loop.http_completer(
                review.base_url, review.model, review.key_file, provider="ditto"
            ) as complete:
                result = agent_loop.review(
                    session, complete=complete, ledger=ledger, deadline_seconds=left
                )
        except Exception:  # noqa: BLE001 - report-only; never echo error text
            emit({"review": summarize_review(None, status="error")})
            return
        emit({"review": summarize_review(result, status=_review_status(result))})
    finally:
        session.stop()
