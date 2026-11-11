"""Reference miner: validation, provisioning, versioning, isolation, cache, parity."""

from __future__ import annotations

import base64
import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import pytest

from query_adapter_pilot.contract import (
    CONTRACT_VERSION,
    DIM,
    MAX_ADAPT_BODY_BYTES,
    MAX_PROVISION_BODY_BYTES,
    PARITY_MAX_ABS,
    PARITY_MIN_COSINE,
    ROUTE_ADAPT,
    ROUTE_HEALTH,
    ROUTE_PROVISION,
    TOKEN_HEADER,
    AdapterRef,
    AdaptRequest,
    AdaptResponse,
    ErrorCode,
    ProvisionRequest,
    encode_f32,
    make_request_id,
)
from query_adapter_pilot.miner import MinerConfig, RunningMiner, start_miner
from query_adapter_pilot.reference import apply, parity, synthetic_adapter, synthetic_query

TOKEN = "unit-test-token"


def call(
    url: str,
    method: str,
    path: str,
    body: Any = None,
    *,
    token: str | None = TOKEN,
    raw: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname or "", u.port, timeout=10)
    try:
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[TOKEN_HEADER] = token
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def provision(url: str, tenant: str, version: int, seed: int) -> tuple[int, dict[str, Any]]:
    w = synthetic_adapter(seed)
    return call(
        url,
        "PUT",
        ROUTE_PROVISION,
        ProvisionRequest(AdapterRef(tenant, version, w.digest()), w).to_wire(),
    )


def adapt_body(
    tenant: str, version: int, seed: int, query_seed: int, seq: int = 0
) -> dict[str, Any]:
    ref = AdapterRef(tenant, version, synthetic_adapter(seed).digest())
    rid = make_request_id("unit", tenant, seq)
    return AdaptRequest(rid, ref, synthetic_query(query_seed)).to_wire()


def adapt(
    url: str, tenant: str, version: int, seed: int, query_seed: int, seq: int = 0
) -> tuple[int, dict[str, Any]]:
    return call(url, "POST", ROUTE_ADAPT, adapt_body(tenant, version, seed, query_seed, seq))


def err(body: dict[str, Any]) -> str:
    return str(body["error"]["code"])


def assert_parity(body: dict[str, Any], seed: int, query_seed: int) -> AdaptResponse:
    resp = AdaptResponse.from_wire(body)
    want = apply(synthetic_adapter(seed), synthetic_query(query_seed))
    cos, max_abs = parity(resp.vector, want)
    assert cos >= PARITY_MIN_COSINE and max_abs <= PARITY_MAX_ABS
    return resp


@pytest.fixture
def miner() -> Iterator[RunningMiner]:
    with start_miner(MinerConfig(token=TOKEN, cache_capacity=8, max_tenants=4)) as m:
        yield m


# ─── Health, auth, routing, size ─────────────────────────────────────────────


def test_health_shape_and_url(miner: RunningMiner) -> None:
    assert miner.url.startswith("http://127.0.0.1:")
    status, body = call(miner.url, "GET", ROUTE_HEALTH)
    assert status == 200
    assert body == miner.stats()
    assert body["contract"] == CONTRACT_VERSION and body["status"] == "ok"
    assert body["tenants"] == 0 and body["adapters"] == 0 and body["fault"] == "none"
    assert set(body["cache"]) == {"capacity", "entries", "hits", "misses", "evictions"}
    assert body["cache"]["capacity"] == 8
    assert body["requests"] == {"adapt_ok": 0, "errors": {}}


@pytest.mark.parametrize("token", [None, "", "wrong-token", TOKEN + "x"])
def test_every_route_requires_token(miner: RunningMiner, token: str | None) -> None:
    for method, path, body in (
        ("GET", ROUTE_HEALTH, None),
        ("POST", ROUTE_ADAPT, adapt_body("syn-a", 1, 1, 1)),
        ("GET", "/nope", None),
    ):
        status, resp = call(miner.url, method, path, body, token=token)
        assert status == 401 and err(resp) == ErrorCode.UNAUTHORIZED
    assert miner.stats()["requests"]["errors"][ErrorCode.UNAUTHORIZED] == 3


def test_unknown_route_is_404_invalid_request(miner: RunningMiner) -> None:
    for method, path in (("GET", "/v1/nope"), ("GET", ROUTE_ADAPT), ("DELETE", ROUTE_PROVISION)):
        status, body = call(miner.url, method, path)
        assert status == 404 and err(body) == ErrorCode.INVALID_REQUEST
        assert body["contract"] == CONTRACT_VERSION


@pytest.mark.parametrize(
    ("path", "method", "limit"),
    [
        (ROUTE_ADAPT, "POST", MAX_ADAPT_BODY_BYTES),
        (ROUTE_PROVISION, "PUT", MAX_PROVISION_BODY_BYTES),
    ],
)
def test_oversized_body_rejected_before_read(
    miner: RunningMiner, path: str, method: str, limit: int
) -> None:
    u = urlsplit(miner.url)
    conn = http.client.HTTPConnection(u.hostname or "", u.port, timeout=10)
    try:
        conn.putrequest(method, path)
        conn.putheader(TOKEN_HEADER, TOKEN)
        conn.putheader("Content-Length", str(limit + 1))
        conn.endheaders()  # no body is ever sent
        resp = conn.getresponse()
        assert resp.status == 413
        assert err(json.loads(resp.read())) == ErrorCode.TOO_LARGE
    finally:
        conn.close()


def test_malformed_json_and_non_object(miner: RunningMiner) -> None:
    status, body = call(miner.url, "POST", ROUTE_ADAPT, raw=b"{not json")
    assert status == 400 and err(body) == ErrorCode.INVALID_REQUEST
    status, body = call(miner.url, "POST", ROUTE_ADAPT, [1, 2])
    assert status == 400 and err(body) == ErrorCode.INVALID_REQUEST


def test_non_ascii_digit_content_length_is_invalid_request(miner: RunningMiner) -> None:
    u = urlsplit(miner.url)
    conn = http.client.HTTPConnection(u.hostname or "", u.port, timeout=10)
    try:
        conn.putrequest("POST", ROUTE_ADAPT)
        conn.putheader(TOKEN_HEADER, TOKEN)
        conn.putheader("Content-Length", "²")  # "²": str.isdigit() but not int()
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
        assert err(json.loads(resp.read())) == ErrorCode.INVALID_REQUEST
    finally:
        conn.close()
    assert miner.stats()["requests"]["errors"] == {ErrorCode.INVALID_REQUEST: 1}


# ─── Adapt request validation ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("contract", "query-adapter/v0", ErrorCode.CONTRACT_MISMATCH),
        ("query", encode_f32(np.ones(DIM - 1) / np.sqrt(DIM - 1)), ErrorCode.BAD_SHAPE),
        ("query", encode_f32(np.full(DIM, np.nan)), ErrorCode.NON_FINITE),
        ("query", encode_f32(np.full(DIM, np.inf)), ErrorCode.NON_FINITE),
        ("query", encode_f32(np.full(DIM, 0.5)), ErrorCode.BAD_NORM),
        ("query", "!!not-base64!!", ErrorCode.INVALID_REQUEST),
        ("tenant_id", "user-42", ErrorCode.INVALID_REQUEST),
        ("adapter_version", 0, ErrorCode.INVALID_REQUEST),
        ("adapter_digest", "ab" * 31, ErrorCode.INVALID_REQUEST),
        ("encoding", "f16", ErrorCode.INVALID_REQUEST),
    ],
)
def test_adapt_rejects_bad_fields(miner: RunningMiner, field: str, value: Any, code: str) -> None:
    assert provision(miner.url, "syn-a", 1, 1)[0] == 200
    body = adapt_body("syn-a", 1, 1, 1) | {field: value}
    status, resp = call(miner.url, "POST", ROUTE_ADAPT, body)
    assert err(resp) == code
    assert status == (400 if code != ErrorCode.UNAUTHORIZED else 401)
    if field != "contract":
        assert resp["request_id"] == body["request_id"]  # echoed when it parsed


def test_bad_request_id_not_echoed(miner: RunningMiner) -> None:
    body = adapt_body("syn-a", 1, 1, 1) | {"request_id": "free-form"}
    status, resp = call(miner.url, "POST", ROUTE_ADAPT, body)
    assert status == 400 and err(resp) == ErrorCode.INVALID_REQUEST
    assert "request_id" not in resp


# ─── Provisioning ────────────────────────────────────────────────────────────


def test_provision_idempotent_and_conflicts(miner: RunningMiner) -> None:
    status, body = provision(miner.url, "syn-a", 3, 1)
    assert status == 200 and body["status"] == "provisioned"
    assert body["tenant_id"] == "syn-a" and body["adapter_version"] == 3
    assert body["adapter_digest"] == synthetic_adapter(1).digest()
    status, body = provision(miner.url, "syn-a", 3, 1)
    assert status == 200 and body["status"] == "unchanged"
    status, body = provision(miner.url, "syn-a", 3, 2)
    assert status == 409 and err(body) == ErrorCode.DIGEST_MISMATCH
    status, body = provision(miner.url, "syn-a", 2, 2)
    assert status == 409 and err(body) == ErrorCode.STALE_VERSION
    assert miner.stats()["tenants"] == 1


def test_provision_body_digest_must_match_weights(miner: RunningMiner) -> None:
    w = synthetic_adapter(1)
    body = ProvisionRequest(AdapterRef("syn-a", 1, w.digest()), w).to_wire()
    body["adapter_digest"] = synthetic_adapter(2).digest()
    status, resp = call(miner.url, "PUT", ROUTE_PROVISION, body)
    assert status == 409 and err(resp) == ErrorCode.DIGEST_MISMATCH
    bad = ProvisionRequest(AdapterRef("syn-a", 1, w.digest()), w).to_wire()
    bad["weights"]["b2"] = encode_f32(np.full(DIM, np.nan))
    status, resp = call(miner.url, "PUT", ROUTE_PROVISION, bad)
    assert status == 400 and err(resp) == ErrorCode.NON_FINITE
    bad["weights"]["b2"] = base64.b64encode(b"\0" * 8).decode()
    status, resp = call(miner.url, "PUT", ROUTE_PROVISION, bad)
    assert status == 400 and err(resp) == ErrorCode.BAD_SHAPE
    assert miner.stats()["tenants"] == 0


def test_capacity_limits_new_tenants_only(miner: RunningMiner) -> None:
    for i in range(4):
        assert provision(miner.url, f"syn-t{i}", 1, i)[0] == 200
    status, body = provision(miner.url, "syn-t9", 1, 9)
    assert status == 507 and err(body) == ErrorCode.CAPACITY
    # Existing tenants can still roll forward at capacity.
    assert provision(miner.url, "syn-t0", 2, 20)[1]["status"] == "provisioned"
    assert miner.stats()["tenants"] == 4 and miner.stats()["adapters"] == 4


# ─── Versioning on adapt ─────────────────────────────────────────────────────


def test_stale_and_missing_versions(miner: RunningMiner) -> None:
    status, body = adapt(miner.url, "syn-a", 1, 1, 1)
    assert status == 404 and err(body) == ErrorCode.MISSING_VERSION
    assert body["request_id"] == make_request_id("unit", "syn-a", 0)
    assert provision(miner.url, "syn-a", 1, 1)[0] == 200
    assert adapt(miner.url, "syn-a", 1, 1, 1)[0] == 200
    assert provision(miner.url, "syn-a", 2, 2)[0] == 200
    status, body = adapt(miner.url, "syn-a", 1, 1, 1)
    assert status == 409 and err(body) == ErrorCode.STALE_VERSION
    status, body = adapt(miner.url, "syn-a", 3, 3, 1)
    assert status == 404 and err(body) == ErrorCode.MISSING_VERSION
    status, body = adapt(miner.url, "syn-a", 2, 2, 1)
    assert status == 200
    assert_parity(body, 2, 1)
    # Retirement also dropped the old cache entry.
    assert all(k[1] == 2 for k in miner.cache_keys() if k[0] == "syn-a")


def test_retired_version_cannot_be_reprovisioned(miner: RunningMiner) -> None:
    assert provision(miner.url, "syn-a", 1, 1)[0] == 200
    assert provision(miner.url, "syn-a", 5, 5)[0] == 200
    status, body = provision(miner.url, "syn-a", 1, 1)
    assert status == 409 and err(body) == ErrorCode.STALE_VERSION


# ─── Tenant isolation ────────────────────────────────────────────────────────


def test_tenant_isolation_same_version_number(miner: RunningMiner) -> None:
    assert provision(miner.url, "syn-a", 1, 101)[0] == 200
    assert provision(miner.url, "syn-b", 1, 202)[0] == 200
    sa, ba = adapt(miner.url, "syn-a", 1, 101, 7)
    sb, bb = adapt(miner.url, "syn-b", 1, 202, 7, seq=1)
    assert sa == sb == 200
    va, vb = assert_parity(ba, 101, 7).vector, assert_parity(bb, 202, 7).vector
    assert parity(va, vb)[0] < PARITY_MIN_COSINE  # genuinely different outputs
    # Tenant B's digest presented under tenant A is a mismatch, never B's adapter.
    status, body = adapt(miner.url, "syn-a", 1, 202, 7)
    assert status == 409 and err(body) == ErrorCode.DIGEST_MISMATCH
    keys = miner.cache_keys()
    assert ("syn-a", 1, synthetic_adapter(101).digest()) in keys
    assert ("syn-b", 1, synthetic_adapter(202).digest()) in keys
    assert len(keys) == len(set(keys)) == 2


def test_same_weights_two_tenants_have_separate_cache_entries(miner: RunningMiner) -> None:
    assert provision(miner.url, "syn-a", 1, 5)[0] == 200
    assert provision(miner.url, "syn-b", 1, 5)[0] == 200
    assert adapt(miner.url, "syn-a", 1, 5, 1)[1]["cache"] == "miss"
    assert adapt(miner.url, "syn-b", 1, 5, 1)[1]["cache"] == "miss"
    assert miner.stats()["cache"]["entries"] == 2


# ─── Bounded LRU ─────────────────────────────────────────────────────────────


def test_bounded_lru_round_robin() -> None:
    with start_miner(MinerConfig(token=TOKEN, cache_capacity=2)) as m:
        tenants = [("syn-a", 11), ("syn-b", 12), ("syn-c", 13)]
        for t, seed in tenants:
            assert provision(m.url, t, 1, seed)[0] == 200
        # Warm a, then a again: miss then hit.
        assert adapt(m.url, "syn-a", 1, 11, 1)[1]["cache"] == "miss"
        assert adapt(m.url, "syn-a", 1, 11, 2)[1]["cache"] == "hit"
        # Round-robin over three tenants with capacity 2 always misses.
        flags = []
        for i in range(6):
            t, seed = tenants[(i + 1) % 3]
            status, body = adapt(m.url, t, 1, seed, i)
            assert status == 200
            assert_parity(body, seed, i)
            flags.append(body["cache"])
            assert m.stats()["cache"]["entries"] <= 2
        assert flags == ["miss"] * 6
        cache = m.stats()["cache"]
        assert cache == {"capacity": 2, "entries": 2, "hits": 1, "misses": 7, "evictions": 5}


# ─── Parity and concurrency ──────────────────────────────────────────────────


def test_output_parity_and_echo(miner: RunningMiner) -> None:
    assert provision(miner.url, "syn-a", 4, 9)[0] == 200
    body = adapt_body("syn-a", 4, 9, 33, seq=12)
    status, resp_body = call(miner.url, "POST", ROUTE_ADAPT, body)
    assert status == 200
    resp = assert_parity(resp_body, 9, 33)
    assert resp.request_id == body["request_id"]
    assert resp.ref == AdapterRef("syn-a", 4, synthetic_adapter(9).digest())
    assert resp.compute_us >= 0 and resp.cache == "miss"
    assert abs(float(np.linalg.norm(resp.vector.astype(np.float64))) - 1.0) < 1e-5
    assert miner.stats()["requests"]["adapt_ok"] == 1


def test_concurrent_multi_tenant_requests_all_correct() -> None:
    tenants = [(f"syn-c{i}", 300 + i) for i in range(5)]
    with start_miner(MinerConfig(token=TOKEN, cache_capacity=3)) as m:
        for t, seed in tenants:
            assert provision(m.url, t, 1, seed)[0] == 200
        failures: list[str] = []
        lock = threading.Lock()

        def worker(k: int) -> None:
            for j in range(10):
                t, seed = tenants[(k + j) % len(tenants)]
                qseed = k * 100 + j
                status, body = adapt(m.url, t, 1, seed, qseed, seq=qseed)
                try:
                    assert status == 200, body
                    resp = assert_parity(body, seed, qseed)
                    assert resp.request_id == make_request_id("unit", t, qseed)
                    assert resp.ref.tenant_id == t
                except AssertionError as exc:
                    with lock:
                        failures.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
        assert not failures
        stats = m.stats()
        assert stats["requests"]["adapt_ok"] == 80
        assert stats["cache"]["hits"] + stats["cache"]["misses"] == 80
        assert stats["cache"]["entries"] <= 3


# ─── Binding ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", ""])
def test_refuses_non_loopback(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        start_miner(MinerConfig(host=host, token=TOKEN))


def test_refuses_bad_settings() -> None:
    for cfg in (
        MinerConfig(token=""),
        MinerConfig(token=TOKEN, fault="explode"),
        MinerConfig(token=TOKEN, cache_capacity=0),
        MinerConfig(token=TOKEN, max_tenants=0),
    ):
        with pytest.raises(ValueError):
            start_miner(cfg)


def test_refuses_non_ascii_token() -> None:
    # A latin-1-decoded header can never equal a non-ASCII token's UTF-8 bytes.
    with pytest.raises(ValueError, match="ASCII"):
        start_miner(MinerConfig(token="tökén"))


def test_localhost_binds_ipv4_loopback() -> None:
    with start_miner(MinerConfig(host="localhost", token=TOKEN)) as m:
        assert m.url.startswith("http://127.0.0.1:")
        assert call(m.url, "GET", ROUTE_HEALTH)[0] == 200
