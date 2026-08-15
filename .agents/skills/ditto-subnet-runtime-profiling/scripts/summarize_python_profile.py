#!/usr/bin/env python3
"""Summarize self and inclusive weights from py-spy profile artifacts."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


def _frame_name(frame: dict[str, Any]) -> str:
    name = str(frame.get("name", "<unknown>"))
    file = frame.get("file")
    line = frame.get("line")
    if file and line is not None:
        return f"{name} ({file}:{line})"
    if file:
        return f"{name} ({file})"
    return name


def _add_stack(
    stack: list[str],
    weight: float,
    self_weights: Counter[str],
    inclusive_weights: Counter[str],
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
) -> tuple[Counter[str], Counter[str], float, int, str]:
    self_weights: Counter[str] = Counter()
    inclusive_weights: Counter[str] = Counter()
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
    return self_weights, inclusive_weights, total, stack_count, "samples"


def _read_speedscope(
    path: Path,
) -> tuple[Counter[str], Counter[str], float, int, str]:
    document = json.loads(path.read_text())
    frames = [_frame_name(frame) for frame in document["shared"]["frames"]]
    self_weights: Counter[str] = Counter()
    inclusive_weights: Counter[str] = Counter()
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
    return self_weights, inclusive_weights, total, stack_count, unit


def _format_weight(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.6g}"


def _render_table(label: str, weights: Counter[str], total: float, limit: int) -> None:
    print(f"\nTOP {label.upper()}")
    print(f"{'WEIGHT':>12} {'PERCENT':>8}  FRAME")
    for frame, weight in weights.most_common(limit):
        percent = (100.0 * weight / total) if total else 0.0
        print(f"{_format_weight(weight):>12} {percent:>7.2f}%  {frame}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize py-spy speedscope JSON or collapsed raw stacks."
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 500:
        parser.error("--limit must be between 1 and 500")
    if not args.profile.is_file():
        parser.error(f"profile does not exist: {args.profile}")

    try:
        if args.profile.read_bytes()[:4096].lstrip()[:1] == b"{":
            result = _read_speedscope(args.profile)
            profile_format = "speedscope"
        else:
            result = _read_collapsed(args.profile)
            profile_format = "collapsed"
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))

    self_weights, inclusive_weights, total, stack_count, unit = result
    print(f"profile: {args.profile}")
    print(f"format: {profile_format}")
    print(f"stacks: {stack_count}")
    print(f"total_weight: {_format_weight(total)}")
    print(f"unit: {unit}")
    _render_table("self", self_weights, total, args.limit)
    _render_table("inclusive", inclusive_weights, total, args.limit)


if __name__ == "__main__":
    main()
