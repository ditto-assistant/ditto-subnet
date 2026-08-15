#!/usr/bin/env python3
"""Summarize and compare self/inclusive weights from py-spy artifacts."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

WeightMap = dict[str, float]
ProfileResult = tuple[WeightMap, WeightMap, float, int, str]


def _frame_name(frame: dict[str, Any], *, include_line: bool) -> str:
    name = str(frame.get("name", "<unknown>"))
    file = frame.get("file")
    line = frame.get("line")
    if include_line and file and line is not None:
        return f"{name} ({file}:{line})"
    if file:
        return f"{name} ({file})"
    return name


def _add_stack(
    stack: list[str],
    weight: float,
    self_weights: MutableMapping[str, float],
    inclusive_weights: MutableMapping[str, float],
) -> None:
    if not math.isfinite(weight) or weight < 0:
        raise ValueError("profile contains an invalid stack weight")
    if not stack:
        stack = ["<no-python-frame>"]
    self_weights[stack[-1]] += weight
    for frame in set(stack):
        inclusive_weights[frame] += weight


def _read_collapsed(
    path: Path,
) -> ProfileResult:
    self_weights: defaultdict[str, float] = defaultdict(float)
    inclusive_weights: defaultdict[str, float] = defaultdict(float)
    total = 0.0
    stack_count = 0
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        stack_text, separator, weight_text = line.rpartition(" ")
        if not separator:
            stack_text = ""
            weight_text = line
        try:
            weight = float(weight_text)
        except ValueError as error:
            problem = "no trailing weight" if not separator else "an invalid weight"
            raise ValueError(f"line {line_number} has {problem}") from error
        stack = [frame for frame in stack_text.split(";") if frame]
        _add_stack(stack, weight, self_weights, inclusive_weights)
        total += weight
        stack_count += 1
    return dict(self_weights), dict(inclusive_weights), total, stack_count, "samples"


def _read_speedscope(
    path: Path,
    *,
    include_line: bool,
) -> ProfileResult:
    document = json.loads(path.read_text())
    frames = [
        _frame_name(frame, include_line=include_line)
        for frame in document["shared"]["frames"]
    ]
    self_weights: defaultdict[str, float] = defaultdict(float)
    inclusive_weights: defaultdict[str, float] = defaultdict(float)
    total = 0.0
    stack_count = 0
    units: set[str] = set()
    for profile in document["profiles"]:
        if profile.get("type") != "sampled":
            raise ValueError("only sampled speedscope profiles are supported")
        samples = profile.get("samples", [])
        weights = profile.get("weights")
        if weights is None:
            weights = [1.0] * len(samples)
        if len(weights) != len(samples):
            raise ValueError("speedscope profile has mismatched samples and weights")
        units.add(str(profile.get("unit", "samples")))
        for indexes, raw_weight in zip(samples, weights, strict=True):
            try:
                stack = [frames[index] for index in indexes]
            except (IndexError, TypeError) as error:
                raise ValueError(
                    "speedscope profile has an invalid frame index"
                ) from error
            weight = float(raw_weight)
            _add_stack(stack, weight, self_weights, inclusive_weights)
            total += weight
            stack_count += 1
    unit = ",".join(sorted(units)) if units else "samples"
    return dict(self_weights), dict(inclusive_weights), total, stack_count, unit


def _read_profile(
    path: Path,
    *,
    include_line: bool,
) -> tuple[ProfileResult, str]:
    if path.read_bytes()[:4096].lstrip()[:1] == b"{":
        return _read_speedscope(path, include_line=include_line), "speedscope"
    return _read_collapsed(path), "collapsed"


def _format_weight(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.6g}"


def _render_table(
    label: str,
    weights: Mapping[str, float],
    total: float,
    limit: int,
) -> None:
    print(f"\nTOP {label.upper()}")
    print(f"{'WEIGHT':>12} {'PERCENT':>8}  FRAME")
    ordered = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    for frame, weight in ordered[:limit]:
        percent = (100.0 * weight / total) if total else 0.0
        print(f"{_format_weight(weight):>12} {percent:>7.2f}%  {frame}")


def _render_change_table(
    label: str,
    current: Mapping[str, float],
    current_total: float,
    base: Mapping[str, float],
    base_total: float,
    limit: int,
) -> None:
    changes = []
    for frame in current.keys() | base.keys():
        current_percent = 100.0 * current.get(frame, 0.0) / current_total
        base_percent = 100.0 * base.get(frame, 0.0) / base_total
        changes.append(
            (abs(current_percent - base_percent), current_percent, base_percent, frame)
        )
    changes.sort(key=lambda row: (-row[0], row[3]))

    print(f"\nTOP {label.upper()} CHANGES")
    print(f"{'CURRENT':>9} {'BASE':>9} {'DELTA':>9}  FRAME")
    for _, current_percent, base_percent, frame in changes[:limit]:
        delta = current_percent - base_percent
        print(
            f"{current_percent:>8.2f}% {base_percent:>8.2f}% {delta:>+8.2f}pp  {frame}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize py-spy speedscope JSON or collapsed raw stacks."
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument(
        "--base",
        type=Path,
        help=(
            "baseline artifact for normalized percentage-point comparison; "
            "Speedscope source line numbers are ignored"
        ),
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 500:
        parser.error("--limit must be between 1 and 500")
    if not args.profile.is_file():
        parser.error(f"profile does not exist: {args.profile}")
    if args.base is not None and not args.base.is_file():
        parser.error(f"base profile does not exist: {args.base}")

    try:
        result, profile_format = _read_profile(
            args.profile,
            include_line=args.base is None,
        )
        base_result = None
        base_format = None
        if args.base is not None:
            base_result, base_format = _read_profile(args.base, include_line=False)
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    self_weights, inclusive_weights, total, stack_count, unit = result
    comparison = None
    if args.base is not None and base_result is not None and base_format is not None:
        base_self, base_inclusive, base_total, base_stack_count, base_unit = base_result
        if profile_format != base_format:
            parser.error("profile and base formats differ")
        if unit != base_unit:
            parser.error(f"profile and base units differ: {unit} != {base_unit}")
        if total <= 0 or base_total <= 0:
            parser.error("profile and base must both have positive total weight")
        comparison = (
            base_self,
            base_inclusive,
            base_total,
            base_stack_count,
        )

    print(f"profile: {args.profile}")
    print(f"format: {profile_format}")
    print(f"stacks: {stack_count}")
    print(f"total_weight: {_format_weight(total)}")
    print(f"unit: {unit}")
    _render_table("self", self_weights, total, args.limit)
    _render_table("inclusive", inclusive_weights, total, args.limit)
    if args.base is None or comparison is None:
        return

    base_self, base_inclusive, base_total, base_stack_count = comparison

    print(f"\nbase: {args.base}")
    print(f"base_stacks: {base_stack_count}")
    print(f"base_total_weight: {_format_weight(base_total)}")
    print("comparison: normalized percentage points")
    if profile_format == "speedscope":
        print("frame_identity: function and file (source line ignored)")
    _render_change_table(
        "self",
        self_weights,
        total,
        base_self,
        base_total,
        args.limit,
    )
    _render_change_table(
        "inclusive",
        inclusive_weights,
        total,
        base_inclusive,
        base_total,
        args.limit,
    )


if __name__ == "__main__":
    main()
