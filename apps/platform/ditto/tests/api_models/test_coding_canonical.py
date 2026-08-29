"""Tests for shared Platform coding canonical JSON."""

from __future__ import annotations

import math

import pytest

from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)


def test_canonical_json_sorts_escapes_and_terminates_once() -> None:
    body = coding_canonical_json_bytes(
        {"z": "\u2028\u2029", "a": 1},
        maximum_bytes=1024,
        label="test vector",
    )
    assert body == b'{"a":1,"z":"\\u2028\\u2029"}\n'
    assert coding_canonical_sha256(
        {"a": 1, "z": "\u2028\u2029"},
        maximum_bytes=1024,
        label="test vector",
    ) == coding_canonical_sha256(
        {"z": "\u2028\u2029", "a": 1},
        maximum_bytes=1024,
        label="test vector",
    )


def test_canonical_json_rejects_nonfinite_and_oversized_values() -> None:
    with pytest.raises(ValueError):
        coding_canonical_json_bytes(
            {"value": math.nan},
            maximum_bytes=1024,
            label="test vector",
        )
    with pytest.raises(ValueError, match="exceeds 8 bytes"):
        coding_canonical_json_bytes(
            {"value": "large"},
            maximum_bytes=8,
            label="test vector",
        )
