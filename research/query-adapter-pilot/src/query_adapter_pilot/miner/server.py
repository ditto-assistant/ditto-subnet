"""Loopback-only HTTP server for the reference ``query-adapter/v1`` miner.

The reference miner is the reference math: every successful ``/v1/adapt``
applies ``reference.apply`` with the exact provisioned adapter. It binds
loopback only and every route requires the pilot token.
"""

from __future__ import annotations

import hmac
import json
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Final, cast, override
from urllib.parse import urlsplit

import numpy as np

from .. import reference
from ..contract import (
    CONTRACT_VERSION,
    HTTP_STATUS,
    MAX_ADAPT_BODY_BYTES,
    MAX_PROVISION_BODY_BYTES,
    REQUEST_ID_RE,
    ROUTE_ADAPT,
    ROUTE_HEALTH,
    ROUTE_PROVISION,
    TOKEN_HEADER,
    AdaptRequest,
    AdaptResponse,
    ContractError,
    ErrorCode,
    ProvisionRequest,
    error_body,
)
from .state import FAULT_MODES, MinerState, perturb, wrong_echo

# Host the operator names -> (address actually bound, address family).
LOOPBACK_HOSTS: Final[dict[str, tuple[str, socket.AddressFamily]]] = {
    "127.0.0.1": ("127.0.0.1", socket.AF_INET),
    "localhost": ("127.0.0.1", socket.AF_INET),
    "::1": ("::1", socket.AF_INET6),
}
INTERNAL_ERROR_KEY: Final = "INTERNAL"


@dataclass(frozen=True, kw_only=True)
class MinerConfig:
    host: str = "127.0.0.1"
    port: int = 0
    token: str
    cache_capacity: int = 64
    max_tenants: int = 1024
    fault: str = "none"
    slow_ms: int = 0
    fault_seed: int = 0

    def validate(self) -> None:
        """Raise ``ValueError`` for any unsafe or nonsensical setting."""
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"refusing to bind non-loopback host {self.host!r}; "
                f"allowed: {', '.join(LOOPBACK_HOSTS)}"
            )
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be in [0, 65535]")
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("a non-empty pilot token is required")
        if not self.token.isascii():
            # Header values arrive latin-1 decoded, so a non-ASCII token could never match.
            raise ValueError("the pilot token must be ASCII")
        if self.cache_capacity < 1:
            raise ValueError("cache_capacity must be >= 1")
        if self.max_tenants < 1:
            raise ValueError("max_tenants must be >= 1")
        if self.fault not in FAULT_MODES:
            raise ValueError(f"fault must be one of {', '.join(FAULT_MODES)}")
        if self.slow_ms < 0:
            raise ValueError("slow_ms must be >= 0")
        if self.fault_seed < 0:
            raise ValueError("fault_seed must be >= 0")


class _HttpError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = HTTP_STATUS[code] if status is None else status


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    config: MinerConfig
    state: MinerState
    token_bytes: bytes

    @override
    def server_bind(self) -> None:
        # Skip HTTPServer's reverse-DNS getfqdn() lookup on the bind address.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    @override
    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that hangs up mid-response is not a miner fault; stay quiet.
        return


class _Server6(_Server):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "query-adapter-pilot-miner/1"
    sys_version = ""
    timeout = 30.0

    _pending_body = False

    @property
    def _srv(self) -> _Server:
        return cast(_Server, self.server)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    # ─── Plumbing ────────────────────────────────────────────────────────

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self._pending_body:
            # We never read this request's body, so the stream is unusable.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER)
        if supplied is None:
            return False
        return hmac.compare_digest(supplied.encode("latin-1", "replace"), self._srv.token_bytes)

    def _read_json(self, limit: int) -> Any:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _HttpError(ErrorCode.INVALID_REQUEST, "chunked bodies are not supported")
        length = self.headers.get("Content-Length")
        value = "" if length is None else length.strip()
        # str.isdigit() alone accepts non-ASCII digits such as "²" that int() rejects.
        if not (value.isascii() and value.isdigit()):
            raise _HttpError(ErrorCode.INVALID_REQUEST, "a valid Content-Length is required")
        n = int(value)
        if n > limit:
            raise _HttpError(ErrorCode.TOO_LARGE, f"body is {n} bytes, limit is {limit}")
        data = self.rfile.read(n)
        self._pending_body = False
        if len(data) != n:
            self.close_connection = True
            raise _HttpError(ErrorCode.INVALID_REQUEST, "body is shorter than Content-Length")
        try:
            return json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise _HttpError(ErrorCode.INVALID_REQUEST, "body is not valid JSON") from exc

    def _dispatch(self, method: str) -> None:
        state = self._srv.state
        length = self.headers.get("Content-Length", "0").strip()
        self._pending_body = self.headers.get("Transfer-Encoding") is not None or length not in (
            "",
            "0",
        )
        request_id: str | None = None
        try:
            if not self._authorized():
                raise _HttpError(ErrorCode.UNAUTHORIZED, "missing or wrong pilot token")
            route = (method, urlsplit(self.path).path)
            if route == ("GET", ROUTE_HEALTH):
                self._send(200, state.health())
            elif route == ("PUT", ROUTE_PROVISION):
                preq = ProvisionRequest.from_wire(self._read_json(MAX_PROVISION_BODY_BYTES))
                status = state.provision(preq)
                body = {"contract": CONTRACT_VERSION, "status": status} | preq.ref.to_wire()
                self._send(200, body)
            elif route == ("POST", ROUTE_ADAPT):
                raw = self._read_json(MAX_ADAPT_BODY_BYTES)
                if isinstance(raw, dict):
                    rid = raw.get("request_id")
                    if isinstance(rid, str) and REQUEST_ID_RE.fullmatch(rid):
                        request_id = rid
                resp = self._adapt(AdaptRequest.from_wire(raw))
                state.record_ok()
                self._send(200, resp.to_wire())
            else:
                raise _HttpError(
                    ErrorCode.INVALID_REQUEST, f"unknown route {method} {route[1]}", status=404
                )
        except _HttpError as exc:
            state.record_error(exc.code)
            self._send(exc.status, error_body(exc.code, exc.message, request_id))
        except ContractError as exc:
            state.record_error(exc.code)
            self._send(HTTP_STATUS[exc.code], error_body(exc.code, exc.message, request_id))
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            state.record_error(INTERNAL_ERROR_KEY)
            self.close_connection = True
            self._send(
                500, error_body(ErrorCode.INVALID_REQUEST, "internal miner error", request_id)
            )

    # ─── Adapt ───────────────────────────────────────────────────────────

    def _adapt(self, req: AdaptRequest) -> AdaptResponse:
        cfg = self._srv.config
        state = self._srv.state
        weights, cache = state.resolve(req.ref)
        fault = cfg.fault
        swap_missing = False
        if fault == "swap-tenant":
            other = state.other_live(req.ref.tenant_id)
            if other is not None:
                weights = other
            else:
                swap_missing = True
        if fault == "slow" and cfg.slow_ms > 0:
            time.sleep(cfg.slow_ms / 1000.0)

        started = time.perf_counter_ns()
        vector = reference.apply(weights, req.query)
        compute_us = max(0, (time.perf_counter_ns() - started) // 1000)

        request_id = req.request_id
        if fault == "perturb" or swap_missing:
            vector = perturb(vector, cfg.fault_seed, req.request_id)
        elif fault == "nan":
            vector = vector.copy()
            vector[0] = np.float32(np.nan)
        elif fault == "bad-norm":
            vector = np.asarray(vector * np.float32(1.1), dtype=np.float32)
        elif fault == "wrong-echo":
            request_id = wrong_echo(req.request_id)
        return AdaptResponse(request_id, req.ref, vector, int(compute_us), cache)


class RunningMiner:
    """A miner serving on a daemon thread. Use as a context manager or call
    ``close()``."""

    def __init__(self, server: _Server, thread: threading.Thread, url: str) -> None:
        self._server = server
        self._thread = thread
        self._closed = False
        self._close_lock = threading.Lock()
        self.url = url
        self.config = server.config

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def cache_keys(self) -> list[tuple[str, int, str]]:
        """Current cache keys, LRU first: always ``(tenant_id, version, digest)``."""
        return self._server.state.cache.cached_keys()

    def stats(self) -> dict[str, Any]:
        """Same dict as the ``GET /v1/health`` body."""
        return self._server.state.health()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> RunningMiner:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def start_miner(cfg: MinerConfig) -> RunningMiner:
    """Bind loopback and serve on a daemon thread. Raises ``ValueError`` for a
    non-loopback host or any invalid setting."""
    cfg.validate()
    bind_host, family = LOOPBACK_HOSTS[cfg.host]
    server_cls = _Server6 if family == socket.AF_INET6 else _Server
    server = server_cls((bind_host, cfg.port), _Handler, bind_and_activate=False)
    try:
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.config = cfg
    server.state = MinerState(cfg.cache_capacity, cfg.max_tenants, cfg.fault)
    server.token_bytes = cfg.token.encode("utf-8")
    port = int(server.server_address[1])
    url_host = f"[{bind_host}]" if family == socket.AF_INET6 else bind_host
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name=f"query-adapter-miner:{port}",
        daemon=True,
    )
    thread.start()
    return RunningMiner(server, thread, f"http://{url_host}:{port}")
