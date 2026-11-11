"""reference.apply agrees with an independent float64 re-implementation on
every fixture request, the golden vectors still hold, and the fixtures are
discriminating (a wrong tenant or version would fail parity)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from query_adapter_pilot import fixtures
from query_adapter_pilot.contract import (
    DIM,
    OUTPUT_NORM_TOLERANCE,
    PARITY_MAX_ABS,
    PARITY_MIN_COSINE,
    QUERY_NORM_TOLERANCE,
    AdapterWeights,
)
from query_adapter_pilot.reference import apply, synthetic_adapter, synthetic_query
from query_adapter_pilot.workload import Workload

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "testdata" / "golden_reference_v1.json"
F64 = npt.NDArray[np.float64]


def independent_apply(w: AdapterWeights, query: npt.NDArray[np.float32]) -> F64:
    """Float64 re-derivation of f(q) = normalize(q + a * MLP(q)) with the exact
    tanh-GELU constant sqrt(2/pi); shares no code with reference.apply."""
    x = query.astype(np.float64)
    z = x @ w.W1.astype(np.float64) + w.b1.astype(np.float64)
    h = 0.5 * z * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (z + 0.044715 * z**3)))
    out = x + float(np.float32(w.alpha)) * (h @ w.W2.astype(np.float64) + w.b2.astype(np.float64))
    return np.asarray(out / np.linalg.norm(out), dtype=np.float64)


def cos_abs(a: npt.NDArray[np.floating], b: npt.NDArray[np.floating]) -> tuple[float, float]:
    a64, b64 = np.asarray(a, np.float64), np.asarray(b, np.float64)
    cos = float(a64 @ b64 / (np.linalg.norm(a64) * np.linalg.norm(b64)))
    return cos, float(np.max(np.abs(a64 - b64)))


def fixture_cases() -> list[tuple[str, int, int]]:
    """Unique (workload, adapter seed, query seed) for every version a request
    could legitimately be served with (authorized and provisioned)."""
    cases: set[tuple[str, int, int]] = set()
    for name in fixtures.WORKLOAD_NAMES:
        wl = Workload.load(ROOT / "testdata" / "workloads" / f"{name}.json")
        for r in wl.requests:
            u = wl.user(r.user_ref)
            for v in {*u.provision, *([u.authorized_version] if u.authorized_version else [])}:
                spec = u.adapter(v)
                assert spec is not None
                cases.add((name, spec.seed, r.query_seed))
    return sorted(cases)


CASES = fixture_cases()


def test_case_count_covers_every_workload() -> None:
    assert {c[0] for c in CASES} == set(fixtures.WORKLOAD_NAMES)
    assert len(CASES) >= 200


@pytest.mark.parametrize("workload", fixtures.WORKLOAD_NAMES)
def test_reference_matches_independent_float64(workload: str) -> None:
    adapters: dict[int, AdapterWeights] = {}
    for _, seed, qseed in (c for c in CASES if c[0] == workload):
        w = adapters.setdefault(seed, synthetic_adapter(seed))
        q = synthetic_query(qseed)
        assert abs(float(np.linalg.norm(q.astype(np.float64))) - 1.0) <= QUERY_NORM_TOLERANCE
        got = apply(w, q)
        assert abs(float(np.linalg.norm(got.astype(np.float64))) - 1.0) <= OUTPUT_NORM_TOLERANCE
        cos, max_abs = cos_abs(got, independent_apply(w, q))
        assert cos >= PARITY_MIN_COSINE, (workload, seed, qseed, cos)
        assert max_abs <= PARITY_MAX_ABS, (workload, seed, qseed, max_abs)


def test_golden_reference_v1_still_matches() -> None:
    doc = json.loads(GOLDEN.read_text())
    assert doc["kind"] == "golden-reference" and len(doc["cases"]) == 6
    for case in doc["cases"]:
        w = synthetic_adapter(case["adapter_seed"])
        assert w.digest() == case["adapter_digest"]
        want = np.frombuffer(base64.b64decode(case["expected"]), dtype="<f4")
        assert want.shape == (DIM,)
        q = synthetic_query(case["query_seed"])
        for got in (apply(w, q), independent_apply(w, q)):
            cos, max_abs = cos_abs(got, want)
            assert cos >= PARITY_MIN_COSINE and max_abs <= PARITY_MAX_ABS


def test_fixtures_discriminate_tenant_and_version() -> None:
    """Swapping tenants (isolation) or serving the retired version (stale /
    multi_tenant) must be visible to parity verification."""
    iso = Workload.load(ROOT / "testdata" / "workloads" / "isolation.json")
    q = synthetic_query(iso.requests[0].query_seed)
    outs = []
    for u in iso.users:
        for v in (1, 2):
            spec = u.adapter(v)
            assert spec is not None
            outs.append(apply(spec.weights(), q))
    for i in range(len(outs)):
        for j in range(i + 1, len(outs)):
            assert cos_abs(outs[i], outs[j])[0] < PARITY_MIN_COSINE
    mt = Workload.load(ROOT / "testdata" / "workloads" / "multi_tenant.json")
    u = mt.users[0]
    v1, v2 = u.adapter(1), u.adapter(2)
    assert v1 is not None and v2 is not None and v1.weights().digest() != v2.weights().digest()
    qm = synthetic_query(mt.requests[0].query_seed)
    assert cos_abs(apply(v1.weights(), qm), apply(v2.weights(), qm))[0] < PARITY_MIN_COSINE
