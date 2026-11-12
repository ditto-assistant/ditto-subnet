"""Each test-only fault mode produces exactly its intended defect."""

from __future__ import annotations

import base64
import http.client
import json
import time
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import pytest

from query_adapter_pilot.contract import (
    DIM,
    OUTPUT_NORM_TOLERANCE,
    PARITY_MAX_ABS,
    PARITY_MIN_COSINE,
    REQUEST_ID_RE,
    ROUTE_ADAPT,
    ROUTE_HEALTH,
    ROUTE_PROVISION,
    TOKEN_HEADER,
    AdapterRef,
    AdaptRequest,
    AdaptResponse,
    ErrorCode,
    ProvisionRequest,
    make_request_id,
)
from query_adapter_pilot.miner import FAULT_MODES, MinerConfig, RunningMiner, start_miner
from query_adapter_pilot.reference import apply, parity, synthetic_adapter, synthetic_query

TOKEN = "fault-token"
SEED_A, SEED_B, QUERY = 41, 42, 5


def call(url: str, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname or "", u.port, timeout=10)
    try:
        data = None if body is None else json.dumps(body).encode()
        conn.request(method, path, body=data, headers={TOKEN_HEADER: TOKEN})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def provision(m: RunningMiner, tenant: str, seed: int) -> None:
    w = synthetic_adapter(seed)
    body = ProvisionRequest(AdapterRef(tenant, 1, w.digest()), w).to_wire()
    assert call(m.url, "PUT", ROUTE_PROVISION, body)[0] == 200


def request(tenant: str = "syn-a", seed: int = SEED_A) -> AdaptRequest:
    ref = AdapterRef(tenant, 1, synthetic_adapter(seed).digest())
    return AdaptRequest(make_request_id("fault", tenant, 7), ref, synthetic_query(QUERY))


def run(fault: str, *, tenants: int = 2, **kw: Any) -> tuple[AdaptRequest, dict[str, Any]]:
    with start_miner(MinerConfig(token=TOKEN, fault=fault, **kw)) as m:
        provision(m, "syn-a", SEED_A)
        if tenants > 1:
            provision(m, "syn-b", SEED_B)
        req = request()
        status, body = call(m.url, "POST", ROUTE_ADAPT, req.to_wire())
        assert status == 200
        assert call(m.url, "GET", ROUTE_HEALTH)[1]["fault"] == fault
        return req, body


def _norm(v: np.ndarray[Any, Any]) -> float:
    return float(np.linalg.norm(v.astype(np.float64)))


def _correct() -> np.ndarray[Any, Any]:
    return apply(synthetic_adapter(SEED_A), synthetic_query(QUERY))


def _fails_parity(v: np.ndarray[Any, Any], want: np.ndarray[Any, Any]) -> bool:
    cos, max_abs = parity(v, want)
    return cos < PARITY_MIN_COSINE or max_abs > PARITY_MAX_ABS


def test_none_is_correct() -> None:
    req, body = run("none")
    resp = AdaptResponse.from_wire(body)
    assert resp.request_id == req.request_id and not _fails_parity(resp.vector, _correct())


def test_perturb_passes_norm_fails_parity_and_is_seeded() -> None:
    req, body = run("perturb", fault_seed=3)
    resp = AdaptResponse.from_wire(body)
    assert resp.request_id == req.request_id and resp.ref == req.ref
    assert abs(_norm(resp.vector) - 1.0) <= OUTPUT_NORM_TOLERANCE
    assert _fails_parity(resp.vector, _correct())
    _, again = run("perturb", fault_seed=3)
    _, other = run("perturb", fault_seed=4)
    assert again["vector"] == body["vector"] and other["vector"] != body["vector"]


def test_nan_is_on_the_wire_and_rejected_by_strict_parse() -> None:
    _, body = run("nan")
    raw = np.frombuffer(base64.b64decode(body["vector"]), dtype="<f4")
    assert raw.shape == (DIM,) and int(np.isnan(raw).sum()) == 1
    with pytest.raises(Exception) as exc:
        AdaptResponse.from_wire(body)
    assert getattr(exc.value, "code", None) == ErrorCode.NON_FINITE


def test_bad_norm_scales_correct_output() -> None:
    _, body = run("bad-norm")
    resp = AdaptResponse.from_wire(body)
    assert abs(_norm(resp.vector) - 1.1) < 1e-4
    assert abs(_norm(resp.vector) - 1.0) > OUTPUT_NORM_TOLERANCE
    cos, _ = parity(resp.vector, _correct())
    assert cos >= PARITY_MIN_COSINE  # direction is right, only the norm is wrong


def test_wrong_echo_is_well_formed_but_different() -> None:
    req, body = run("wrong-echo")
    resp = AdaptResponse.from_wire(body)
    assert resp.request_id != req.request_id and REQUEST_ID_RE.fullmatch(resp.request_id)
    assert resp.ref == req.ref and not _fails_parity(resp.vector, _correct())


def test_swap_tenant_uses_other_tenants_adapter_but_echoes_ref() -> None:
    req, body = run("swap-tenant")
    resp = AdaptResponse.from_wire(body)
    assert resp.request_id == req.request_id and resp.ref == req.ref
    other = apply(synthetic_adapter(SEED_B), synthetic_query(QUERY))
    assert not _fails_parity(resp.vector, other)
    assert _fails_parity(resp.vector, _correct())


def test_swap_tenant_with_single_tenant_falls_back_to_perturb() -> None:
    req, body = run("swap-tenant", tenants=1)
    resp = AdaptResponse.from_wire(body)
    assert resp.ref == req.ref and abs(_norm(resp.vector) - 1.0) <= OUTPUT_NORM_TOLERANCE
    assert _fails_parity(resp.vector, _correct())


def test_slow_sleeps_then_answers_correctly() -> None:
    started = time.monotonic()
    req, body = run("slow", slow_ms=250)
    assert time.monotonic() - started >= 0.25
    resp = AdaptResponse.from_wire(body)
    assert resp.request_id == req.request_id and not _fails_parity(resp.vector, _correct())


@pytest.mark.parametrize("fault", [f for f in FAULT_MODES if f != "none"])
def test_faults_never_touch_error_responses(fault: str) -> None:
    with start_miner(MinerConfig(token=TOKEN, fault=fault, slow_ms=1)) as m:
        req = request()
        status, body = call(m.url, "POST", ROUTE_ADAPT, req.to_wire())
        assert status == 404 and body["error"]["code"] == ErrorCode.MISSING_VERSION
        assert body["request_id"] == req.request_id
