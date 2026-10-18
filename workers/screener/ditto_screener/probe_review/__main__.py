"""CLI for the report-only probe reviewer.

One invocation per reviewer tool call (``PR = python -m
ditto_screener.probe_review``)::

    PR prepare --archive A.tar.gz --sha256 S --out DIR
    PR start   --session DIR
    PR run     --session DIR --request @req.json --script @script.json
    PR show    --session DIR --probe p004-ab12cd [--entry 0]
    PR batch   --session DIR --plan @plan.json
    PR mutate  --session DIR --probe ID --transform rename_tools
    PR diff    --session DIR A B
    PR read    --session DIR --path src/v13.rs --start 100 --end 180
    PR search  --session DIR --pattern 'trim_matches'
    PR verdict --session DIR --verdict @verdict.json
    PR stop    --session DIR

Nothing here contacts Platform; outputs are shadow evidence only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from ditto_screener.probe_review import battery, tools
from ditto_screener.probe_review.sandbox import Session, build_image


def _json_arg(value: str | None) -> Any:
    if value is None:
        return None
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text())
    return json.loads(value)


def _triage(archive: Path) -> dict[str, Any]:
    """Zero-token static triage: the shadow semantic-discovery location hints."""
    from ditto_screener.fanout_discovery import semantic_discovery

    try:
        return semantic_discovery(str(archive), max_hints=12)
    except Exception as error:  # noqa: BLE001 - triage is advisory
        return {"error": type(error).__name__, "leads": []}


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    archive = Path(args.archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != args.sha256.lower():
        raise SystemExit(f"archive SHA mismatch: {digest}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    started = time.monotonic()
    image, build_seconds = build_image(archive, f"probe-review/{digest[:16]}")
    timings["build_s"] = round(build_seconds, 1)
    session = Session(
        root=str(out),
        image=image,
        artifact_sha256=digest,
        bench_version=args.bench_version,
    )
    files = tools.extract_source(archive, out / "src")
    t0 = time.monotonic()
    triage = _triage(archive)
    timings["triage_s"] = round(time.monotonic() - t0, 2)
    t0 = time.monotonic()
    session.start()
    timings["start_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    report = battery.run_battery(session)
    timings["battery_s"] = round(time.monotonic() - t0, 1)
    if args.keep:
        session.restart_harness()
    else:
        session.stop()
    timings["total_s"] = round(time.monotonic() - started, 1)
    result = {
        "artifact_sha256": digest,
        "image": image,
        "source_files": files,
        "timings": timings,
        "triage": triage,
        "battery": report,
    }
    (out / "prepare.json").write_text(json.dumps(result, indent=1))
    return {
        "session": str(out),
        "timings": timings,
        "format": report["format"],
        "battery_leads": [
            {
                k: lead.get(k)
                for k in ("item_id", "invariant", "probe_id", "observed", "note")
            }
            for lead in report["leads"]
        ],
        "battery_items": [
            {
                k: item.get(k)
                for k in (
                    "item_id",
                    "applicable",
                    "lead",
                    "probe_id",
                    "model_calls",
                    "tool_executions",
                )
            }
            for item in report["items"]
        ],
        "triage_leads": [
            {"kind": lead.get("kind"), "locations": lead.get("locations")}
            for lead in triage.get("leads", [])
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="probe_review")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--archive", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--bench-version", type=int, default=13)
    p.add_argument("--keep", action="store_true", help="leave the harness running")
    for name in (
        "run",
        "show",
        "batch",
        "mutate",
        "diff",
        "read",
        "search",
        "verdict",
        "stop",
        "restart",
        "start",
    ):
        q = sub.add_parser(name)
        q.add_argument("--session", required=True)
        if name == "run":
            q.add_argument("--request", required=True)
            q.add_argument("--script")
            q.add_argument("--seed")
            q.add_argument("--checks")
            q.add_argument("--path", default="/run")
            q.add_argument("--label", default="")
        if name == "show":
            q.add_argument("--probe", required=True)
            q.add_argument("--entry", type=int)
            q.add_argument("--chars", type=int, default=4000)
        if name == "batch":
            q.add_argument("--plan", required=True)
        if name == "mutate":
            q.add_argument("--probe", required=True)
            q.add_argument("--transform", required=True)
            q.add_argument("--value")
            q.add_argument("--path")
        if name == "diff":
            q.add_argument("a")
            q.add_argument("b")
        if name == "read":
            q.add_argument("--path", required=True)
            q.add_argument("--start", type=int, default=1)
            q.add_argument("--end", type=int)
        if name == "search":
            q.add_argument("--pattern", required=True)
            q.add_argument("--glob", default="*")
        if name == "verdict":
            q.add_argument("--verdict", required=True)
            q.add_argument("--replays", type=int, default=1)
    args = parser.parse_args(argv)
    if args.cmd == "prepare":
        result: Any = prepare(args)
    else:
        session = Session.load(args.session)
        if args.cmd == "run":
            result = tools.run_case(
                session,
                _json_arg(args.request),
                _json_arg(args.script),
                path=args.path,
                label=args.label,
                seed=_json_arg(args.seed),
                checks=_json_arg(args.checks),
            )
        elif args.cmd == "show":
            result = tools.show(session, args.probe, entry=args.entry, chars=args.chars)
        elif args.cmd == "batch":
            result = tools.batch(session, _json_arg(args.plan))
        elif args.cmd == "mutate":
            value: Any = args.value
            if value is not None:
                with contextlib.suppress(ValueError):
                    value = json.loads(value)
            result = tools.mutate(session, args.probe, args.transform, value, args.path)
        elif args.cmd == "diff":
            result = tools.diff(session, args.a, args.b)
        elif args.cmd == "read":
            result = tools.read_file(session, args.path, args.start, args.end)
        elif args.cmd == "search":
            result = tools.search(session, args.pattern, args.glob)
        elif args.cmd == "verdict":
            verdict = _json_arg(args.verdict)
            result = tools.replay_verdict(session, verdict, replays=args.replays)
            Path(args.session, "verdict.json").write_text(json.dumps(verdict, indent=1))
            Path(args.session, "replay.json").write_text(json.dumps(result, indent=1))
        elif args.cmd == "start":
            if session.started:
                session.stop()
            session.start()
            result = {"started": True}
        elif args.cmd == "restart":
            session.restart_harness()
            result = {"restarted": True}
        else:
            session.stop()
            result = {"stopped": True}
    json.dump(result, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
