"""The reference math matches the source QueryAdapter.apply on synthetic data."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from query_adapter_pilot.contract import DIM, PARITY_MAX_ABS, PARITY_MIN_COSINE, decode_f32
from query_adapter_pilot.reference import (
    apply,
    identity,
    parity,
    synthetic_adapter,
    synthetic_query,
)

GOLDEN = Path(__file__).resolve().parents[1] / "testdata" / "golden_reference_v1.json"


def test_matches_source_golden_vectors() -> None:
    doc = json.loads(GOLDEN.read_text())
    assert doc["source"]["callable"] == "QueryAdapter.apply"
    assert len(doc["cases"]) == 6
    for case in doc["cases"]:
        w = synthetic_adapter(case["adapter_seed"])
        assert w.digest() == case["adapter_digest"]
        got = apply(w, synthetic_query(case["query_seed"]))
        want = decode_f32(case["expected"], (DIM,), "expected")
        cos, max_abs = parity(got, want)
        assert cos >= PARITY_MIN_COSINE
        assert max_abs <= PARITY_MAX_ABS


def test_output_is_unit_norm_and_differs_per_adapter() -> None:
    q = synthetic_query(7)
    a, b = apply(synthetic_adapter(1), q), apply(synthetic_adapter(2), q)
    assert a.dtype == np.float32 and a.shape == (DIM,)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    assert parity(a, b)[0] < PARITY_MIN_COSINE
    assert parity(a, q)[0] < PARITY_MIN_COSINE


def test_generators_are_deterministic() -> None:
    assert synthetic_adapter(3).digest() == synthetic_adapter(3).digest()
    assert synthetic_adapter(3).digest() != synthetic_adapter(4).digest()
    assert np.array_equal(synthetic_query(5), synthetic_query(5))
    assert np.array_equal(identity(synthetic_query(5)), synthetic_query(5))
