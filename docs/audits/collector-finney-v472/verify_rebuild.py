"""Compare expanded WASM and independently produced WAT; evidence only.

Usage: python verify_rebuild.py LIVE.wasm REBUILT.wasm LIVE.wat REBUILT.wat
Expand the Substrate compressed files and produce WAT with pinned wabt1.0.42.
This script never authorizes a runtime hash or normalizes a production check.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SEED_NAME = (
    "_ZN83_$LT$wasmi_collections..hash..RandomStateImpl$u20$as$u20$core..default.."
    "Default$GT$7default"
)


def leb(data, offset):
    value = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
        shift += 7
        if shift >= 35:
            raise ValueError("invalid WASM length")


def sections(data):
    if data[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("expanded WASM required")
    offset = 8
    result = []
    while offset < len(data):
        kind = data[offset]
        size, start = leb(data, offset + 1)
        body = data[start : start + size]
        if len(body) != size:
            raise ValueError("truncated section")
        result.append((kind, body))
        offset = start + size
    return result


def function_bodies(code):
    count, offset = leb(code, 0)
    result = []
    for _ in range(count):
        size, start = leb(code, offset)
        body = code[start : start + size]
        if len(body) != size:
            raise ValueError("truncated function")
        result.append(body)
        offset = start + size
    if offset != len(code):
        raise ValueError("unexpected code suffix")
    return result


def function_wat(text):
    start = text.index("  (func $" + SEED_NAME)
    end = text.index("\n  (func ", start + 5)
    return text[start:end].splitlines()


def verify(live, rebuilt, live_wat, rebuilt_wat):
    first, second = sections(live), sections(rebuilt)
    if len(first) != len(second):
        raise ValueError("section count differs")
    changed = []
    function_count = 0
    for (kind, body), (other_kind, other_body) in zip(first, second, strict=True):
        if kind != other_kind:
            raise ValueError("section kinds differ")
        if kind != 10:
            if body != other_body:
                raise ValueError("non-code section differs")
            continue
        a, b = function_bodies(body), function_bodies(other_body)
        if len(a) != len(b):
            raise ValueError("function count differs")
        function_count = len(a)
        changed = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    if len(changed) != 1:
        raise ValueError("expected one isolated differing function")
    a, b = function_wat(live_wat), function_wat(rebuilt_wat)
    if len(a) != len(b):
        raise ValueError("seed instruction count differs")
    constants = []
    for number, (x, y) in enumerate(zip(a, b, strict=True), 1):
        if x == y:
            continue
        if not all(re.fullmatch(r"\s*i64.const -?\d+", v) for v in (x, y)):
            raise ValueError("seed instruction differs beyond integer constant")
        constants.append(number)
    if len(constants) != 22:
        raise ValueError("unexpected seed constant changes")
    # No other instruction or function may differ in the WAT either. Comparing
    # both representations prevents an unrelated binary body being excused by
    # a matching textual fragment supplied for another function.
    if live_wat.replace(
        "\n".join(a), "<isolated-seed-function>", 1
    ) != rebuilt_wat.replace("\n".join(b), "<isolated-seed-function>", 1):
        raise ValueError("other WAT differs")
    return {
        "function_body_count": function_count,
        "differing_code_body_index": changed[0],
        "changed_i64_const_instructions": len(constants),
        "other_function_bodies_and_sections_identical": True,
        "unmodified_rebuild_byte_identical": False,
        "authority": "none; evidence only",
    }


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    paths = list(map(Path, sys.argv[1:]))
    print(
        json.dumps(
            verify(
                paths[0].read_bytes(),
                paths[1].read_bytes(),
                paths[2].read_text(),
                paths[3].read_text(),
            ),
            indent=2,
        )
    )
