#!/usr/bin/env python3
"""Aggregate bounded Caddy JSON access logs without printing request URIs."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RouteMatcher:
    label: str
    pattern: re.Pattern[str]


@dataclass
class RouteStats:
    requests: int = 0
    status_counts: Counter[int] = field(default_factory=Counter)
    success_durations: list[float] = field(default_factory=list)
    uri_counts: Counter[str] = field(default_factory=Counter)

    def add(self, *, method: str, uri: str, status: int, duration: float) -> None:
        self.requests += 1
        self.status_counts[status] += 1
        self.uri_counts[f"{method} {uri}"] += 1
        if 200 <= status < 300:
            self.success_durations.append(duration)


def _route_matcher(value: str) -> RouteMatcher:
    label, separator, expression = value.partition("=")
    if not separator or not label or not expression:
        raise argparse.ArgumentTypeError("route match must use LABEL=REGEX")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", label):
        raise argparse.ArgumentTypeError(f"invalid route label: {label}")
    try:
        pattern = re.compile(expression)
    except re.error as error:
        raise argparse.ArgumentTypeError(
            f"invalid regex for {label}: {error}"
        ) from error
    return RouteMatcher(label=label, pattern=pattern)


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _timestamp(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"invalid timestamp: {value}; use Unix seconds or RFC3339"
            ) from error
        if parsed.tzinfo is None:
            raise argparse.ArgumentTypeError(
                f"invalid timestamp: {value}; RFC3339 timezone is required"
            ) from None
        result = parsed.timestamp()
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError(f"invalid timestamp: {value}")
    return result


def _record_timestamp(record: object, line_number: int) -> float:
    if not isinstance(record, dict):
        raise ValueError(f"line {line_number} is not a JSON object")
    timestamp = record.get("ts")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise ValueError(f"line {line_number} has an invalid timestamp")
    timestamp = float(timestamp)
    if not math.isfinite(timestamp):
        raise ValueError(f"line {line_number} has an invalid timestamp")
    return timestamp


def _extract_request(record: object, line_number: int) -> tuple[str, str, int, float]:
    if not isinstance(record, dict):
        raise ValueError(f"line {line_number} is not a JSON object")
    request = record.get("request")
    if not isinstance(request, dict):
        raise ValueError(f"line {line_number} has no request object")
    method = request.get("method")
    uri = request.get("uri")
    status = record.get("status")
    duration = record.get("duration")
    if not isinstance(method, str) or not isinstance(uri, str):
        raise ValueError(f"line {line_number} has an invalid request method or URI")
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not 100 <= status <= 599
    ):
        raise ValueError(f"line {line_number} has an invalid status")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise ValueError(f"line {line_number} has an invalid duration")
    duration = float(duration)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"line {line_number} has an invalid duration")
    return method, uri, status, duration


def _read_records(
    stream: TextIO,
    matchers: list[RouteMatcher],
    *,
    since: float | None,
    until: float | None,
) -> tuple[dict[str, RouteStats], int, int]:
    stats = {matcher.label: RouteStats() for matcher in matchers}
    parsed = 0
    selected = 0
    for line_number, raw_line in enumerate(stream, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"line {line_number} is not valid JSON: {error.msg}"
            ) from error
        parsed += 1
        if since is not None or until is not None:
            timestamp = _record_timestamp(record, line_number)
            if since is not None and timestamp < since:
                continue
            if until is not None and timestamp >= until:
                continue
        selected += 1
        method, uri, status, duration = _extract_request(record, line_number)
        path = urlsplit(uri).path
        for matcher in matchers:
            if matcher.pattern.search(path):
                stats[matcher.label].add(
                    method=method,
                    uri=uri,
                    status=status,
                    duration=duration,
                )
    return stats, parsed, selected


def _summary(label: str, stats: RouteStats) -> dict[str, object]:
    durations_ms = [duration * 1000.0 for duration in stats.success_durations]
    requests_per_uri = [float(count) for count in stats.uri_counts.values()]
    return {
        "route": label,
        "requests": stats.requests,
        "success_2xx": len(stats.success_durations),
        "status_counts": {
            str(status): count for status, count in sorted(stats.status_counts.items())
        },
        "latency_2xx_ms": {
            "p50": _nearest_rank(durations_ms, 0.50),
            "p95": _nearest_rank(durations_ms, 0.95),
            "max": max(durations_ms, default=None),
        },
        "unique_uris": len(stats.uri_counts),
        "requests_per_uri": {
            "p50": _nearest_rank(requests_per_uri, 0.50),
            "p95": _nearest_rank(requests_per_uri, 0.95),
            "max": max(requests_per_uri, default=None),
        },
    }


def _format_number(value: object) -> str:
    if value is None:
        return "-"
    assert isinstance(value, (int, float))
    return f"{value:.1f}"


def _render_table(
    summaries: list[dict[str, object]],
    parsed: int,
    selected: int,
) -> None:
    print(f"parsed_records: {parsed}")
    print(f"window_records: {selected}")
    print(
        f"{'ROUTE':<24} {'REQ':>6} {'2XX':>6} {'P50MS':>8} {'P95MS':>8} "
        f"{'MAXMS':>8} {'URIS':>6} {'REQ/URI P95':>11}  STATUS"
    )
    for summary in summaries:
        latency = summary["latency_2xx_ms"]
        per_uri = summary["requests_per_uri"]
        assert isinstance(latency, dict) and isinstance(per_uri, dict)
        statuses = summary["status_counts"]
        assert isinstance(statuses, dict)
        requests = summary["requests"]
        successes = summary["success_2xx"]
        unique_uris = summary["unique_uris"]
        assert isinstance(requests, int)
        assert isinstance(successes, int)
        assert isinstance(unique_uris, int)
        status_text = (
            ",".join(f"{key}:{value}" for key, value in statuses.items()) or "-"
        )
        print(
            f"{str(summary['route']):<24} {requests:>6} "
            f"{successes:>6} "
            f"{_format_number(latency['p50']):>8} "
            f"{_format_number(latency['p95']):>8} "
            f"{_format_number(latency['max']):>8} "
            f"{unique_uris:>6} "
            f"{_format_number(per_uri['p95']):>11}  {status_text}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize Caddy JSON access logs by route without printing raw URIs. "
            "Latency statistics include successful 2xx requests only."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Caddy JSONL file; omit to read stdin",
    )
    parser.add_argument(
        "--match",
        action="append",
        type=_route_matcher,
        default=[],
        metavar="LABEL=REGEX",
        help="route label and regex matched against the URI path; repeatable",
    )
    parser.add_argument(
        "--since",
        type=_timestamp,
        help="inclusive Unix-seconds or RFC3339 lower bound for Caddy's ts field",
    )
    parser.add_argument(
        "--until",
        type=_timestamp,
        help="exclusive Unix-seconds or RFC3339 upper bound for Caddy's ts field",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    matchers = args.match or [RouteMatcher(label="all", pattern=re.compile(".*"))]
    labels = [matcher.label for matcher in matchers]
    if len(labels) != len(set(labels)):
        parser.error("route labels must be unique")
    if args.input is not None and not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    if args.since is not None and args.until is not None and args.since >= args.until:
        parser.error("--since must be earlier than --until")

    stream = args.input.open() if args.input is not None else sys.stdin
    try:
        stats, parsed, selected = _read_records(
            stream,
            matchers,
            since=args.since,
            until=args.until,
        )
    except ValueError as error:
        parser.error(str(error))
    finally:
        if args.input is not None:
            stream.close()

    summaries = [_summary(matcher.label, stats[matcher.label]) for matcher in matchers]
    if args.json:
        print(
            json.dumps(
                {
                    "parsed_records": parsed,
                    "window_records": selected,
                    "window": {"since": args.since, "until": args.until},
                    "routes": summaries,
                },
                indent=2,
            )
        )
    else:
        _render_table(summaries, parsed, selected)


if __name__ == "__main__":
    main()
