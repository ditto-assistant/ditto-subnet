"""A small stdlib loopback fake miner speaking ``query-adapter/v1``, plus
self-tests. The real reference miner lives in another slice; client and pilot
tests use this fake so they exercise the wire, not an in-process shortcut.

Modes (mutable at runtime through ``FakeMiner.mode``):

* ``honest``: correct adapter output.
* ``perturb``: unit-norm but wrong vector (fails parity only).
* ``nan`` / ``bad_norm`` / ``garbage`` / ``deep_json``: malformed output.
* ``wrong_request_id`` / ``wrong_tenant`` / ``wrong_version`` / ``wrong_digest``: bad echo.
* ``slow``: sleeps ``delay_s`` before answering.
* ``error:<CODE>``: answers every adapt call with that contract error.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np
import pytest

from query_adapter_pilot import reference
from query_adapter_pilot.contract import (
    CONTRACT_VERSION,
    HTTP_STATUS,
    ROUTE_ADAPT,
    ROUTE_PROVISION,
    TOKEN_HEADER,
    AdapterRef,
    AdapterWeights,
    AdaptRequest,
    AdaptResponse,
    ContractError,
    ErrorCode,
    ProvisionRequest,
    error_body,
)

TOKEN = "fake-pilot-token"


class FakeMiner:
    def __init__(self, mode: str = "honest", token: str = TOKEN, delay_s: float = 0.5) -> None:
        self.mode = mode
        self.token = token
        self.delay_s = delay_s
        self.adapters: dict[tuple[str, int], AdapterWeights] = {}
        self.newest: dict[str, int] = {}
        self.warm: set[tuple[str, int]] = set()
        self.calls: Counter[str] = Counter()
        self.seen_bodies: list[str] = []
        self._lock = threading.Lock()
        miner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_PUT(self) -> None:
                miner._dispatch(self, "PUT")

            def do_POST(self) -> None:
                miner._dispatch(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> FakeMiner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ─── Handling ────────────────────────────────────────────────────────────

    def _count(self, route: str) -> None:
        with self._lock:
            self.calls[route] += 1

    def _dispatch(self, h: BaseHTTPRequestHandler, method: str) -> None:
        raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
        with self._lock:
            self.seen_bodies.append(raw.decode(errors="replace"))
        try:
            if h.headers.get(TOKEN_HEADER) != self.token:
                raise ContractError(ErrorCode.UNAUTHORIZED, "bad pilot token")
            body = json.loads(raw)
            if method == "PUT" and h.path == ROUTE_PROVISION:
                self._count("provision")
                status, out = self._provision(body)
            elif method == "POST" and h.path == ROUTE_ADAPT:
                self._count("adapt")
                status, out = self._adapt(body)
            else:
                status, out = 404, {"error": "no route"}
        except ContractError as exc:
            status, out = HTTP_STATUS[exc.code], error_body(exc.code, exc.message)
        except ValueError:
            status, out = 400, error_body(ErrorCode.INVALID_REQUEST, "bad json")
        if self.mode == "slow" and h.path == ROUTE_ADAPT:
            time.sleep(self.delay_s)
        payload = out if isinstance(out, bytes) else json.dumps(out).encode()
        try:
            h.send_response(status)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(payload)))
            h.end_headers()
            h.wfile.write(payload)
        except OSError:
            pass  # client gave up (timeout tests)

    def _provision(self, body: Any) -> tuple[int, Any]:
        req = ProvisionRequest.from_wire(body)
        key = (req.ref.tenant_id, req.ref.adapter_version)
        with self._lock:
            newest = self.newest.get(req.ref.tenant_id, 0)
            if req.ref.adapter_version < newest:
                raise ContractError(ErrorCode.STALE_VERSION, "older than newest")
            existing = self.adapters.get(key)
            if existing is not None and existing.digest() != req.ref.adapter_digest:
                raise ContractError(ErrorCode.DIGEST_MISMATCH, "digest differs")
            self.adapters[key] = req.weights
            self.newest[req.ref.tenant_id] = req.ref.adapter_version
        return 200, {"contract": CONTRACT_VERSION, **req.ref.to_wire(), "status": "provisioned"}

    def _adapt(self, body: Any) -> tuple[int, Any]:
        if self.mode.startswith("error:"):
            code = self.mode.split(":", 1)[1]
            raise ContractError(code, "forced error")
        req = AdaptRequest.from_wire(body)
        key = (req.ref.tenant_id, req.ref.adapter_version)
        with self._lock:
            if req.ref.adapter_version < self.newest.get(req.ref.tenant_id, 0):
                raise ContractError(ErrorCode.STALE_VERSION, "retired")
            weights = self.adapters.get(key)
            if weights is None:
                raise ContractError(ErrorCode.MISSING_VERSION, "never provisioned")
            if weights.digest() != req.ref.adapter_digest:
                raise ContractError(ErrorCode.DIGEST_MISMATCH, "digest differs")
            cache = "hit" if key in self.warm else "miss"
            self.warm.add(key)
        vec = reference.apply(weights, req.query)
        rid, ref = req.request_id, req.ref
        mode = self.mode
        if mode == "perturb":
            v = vec + np.float32(0.01) * reference.synthetic_query(12345)
            vec = (v / np.linalg.norm(v)).astype(np.float32)
        elif mode == "nan":
            vec = vec.copy()
            vec[3] = np.float32("nan")
        elif mode == "bad_norm":
            vec = (vec * np.float32(1.01)).astype(np.float32)
        elif mode == "wrong_request_id":
            rid = rid.rsplit(".", 1)[0] + ".999"
        elif mode == "wrong_tenant":
            ref = AdapterRef("syn-someone-else", ref.adapter_version, ref.adapter_digest)
        elif mode == "wrong_version":
            ref = AdapterRef(ref.tenant_id, ref.adapter_version + 1, ref.adapter_digest)
        elif mode == "wrong_digest":
            ref = AdapterRef(ref.tenant_id, ref.adapter_version, "0" * 64)
        elif mode == "garbage":
            return 200, b"not json {"
        elif mode == "deep_json":
            # Well under the 1 MiB cap, but deep enough to blow json's recursion.
            return 200, b"[" * 100_000 + b"]" * 100_000
        return 200, AdaptResponse(rid, ref, vec, 42, cache).to_wire()


def closed_port_url() -> str:
    """A loopback URL nothing listens on (connection refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def workload_json(
    users: list[dict[str, Any]], requests: list[dict[str, Any]], name: str = "test-workload"
) -> dict[str, Any]:
    return {
        "contract": CONTRACT_VERSION,
        "kind": "synthetic-workload",
        "name": name,
        "users": users,
        "requests": requests,
    }


def _seed(ref: str, version: int) -> int:
    return int(hashlib.sha256(f"{ref}|{version}".encode()).hexdigest()[:6], 16)


def user(
    ref: str,
    provision: list[int] | None = None,
    authorized: int | None = 1,
    versions: tuple[int, ...] = (1, 2),
) -> dict[str, Any]:
    return {
        "user_ref": ref,
        "adapters": [{"version": v, "seed": _seed(ref, v)} for v in versions],
        "provision": [1] if provision is None else provision,
        "authorized_version": authorized,
    }


# ─── Self-tests: the fake speaks the contract ────────────────────────────────


def test_fake_miner_rejects_bad_token_and_unknown_versions() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with FakeMiner() as miner:
        w = reference.synthetic_adapter(1)
        ref = AdapterRef("syn-t1", 1, w.digest())
        body = json.dumps(
            AdaptRequest("qa1.r.syn-t1.0", ref, reference.synthetic_query(1)).to_wire()
        )
        req = urllib.request.Request(
            miner.url + ROUTE_ADAPT,
            data=body.encode(),
            method="POST",
            headers={TOKEN_HEADER: TOKEN},
        )
        try:
            opener.open(req, timeout=2)
            raise AssertionError("expected MISSING_VERSION")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert json.loads(exc.read())["error"]["code"] == ErrorCode.MISSING_VERSION
        req.headers = {}
        req.add_header(TOKEN_HEADER, "wrong")
        try:
            opener.open(req, timeout=2)
            raise AssertionError("expected UNAUTHORIZED")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401


def test_closed_port_refuses() -> None:
    url = closed_port_url()
    port = int(url.rsplit(":", 1)[1])
    with socket.socket() as s, pytest.raises(ConnectionRefusedError):
        s.connect(("127.0.0.1", port))
