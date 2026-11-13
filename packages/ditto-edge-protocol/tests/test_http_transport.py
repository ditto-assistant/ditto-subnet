"""The signed HTTP transport over a real loopback socket."""

from __future__ import annotations

import json
import random
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ditto_edge_protocol import paths
from ditto_edge_protocol.coordinator import Coordinator, CoordinatorError
from ditto_edge_protocol.limits import GIB
from ditto_edge_protocol.node import EdgeNode, NodeConfig
from ditto_edge_protocol.settings import LaneSettings
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.sources import synthetic_objects
from ditto_edge_protocol.store import MemoryStore
from ditto_edge_protocol.transport import HttpTransport, TransportError
from ditto_edge_protocol.wire import (
    AckRequest,
    ChallengeAnswer,
    ClientInfo,
    EnrollRequest,
    LeaveRequest,
    PollRequest,
)

_STATUS = {"unauthorized": 401, "forbidden": 403, "not_found": 404, "gone": 410}


def _server(coordinator: Coordinator) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _reply(
            self, status: int, body: bytes, kind: str = "application/json"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path
            try:
                node_id = coordinator.authenticate(
                    method=method, path=path, headers=dict(self.headers), body=body
                )
                if path == paths.ENROLL:
                    out = coordinator.enroll(EnrollRequest.model_validate_json(body))
                elif path == paths.POLL:
                    out = coordinator.poll(
                        node_id, PollRequest.model_validate_json(body)
                    )
                elif path == paths.ACK:
                    out = coordinator.ack(node_id, AckRequest.model_validate_json(body))
                elif path == paths.STATUS:
                    out = coordinator.status(node_id)
                elif path == paths.LEAVE:
                    coordinator.leave(node_id, LeaveRequest.model_validate_json(body))
                    self._reply(204, b"")
                    return
                elif path.startswith(f"{paths.PREFIX}/objects/"):
                    data = coordinator.fill(node_id, path.rsplit("/", 1)[1])
                    self._reply(200, data, "application/octet-stream")
                    return
                elif path.startswith(f"{paths.PREFIX}/challenges/"):
                    out = coordinator.answer(
                        node_id,
                        path.rsplit("/", 1)[1],
                        ChallengeAnswer.model_validate_json(body),
                    )
                else:
                    self._reply(404, b"{}")
                    return
            except CoordinatorError as exc:
                status = _STATUS.get(exc.code, 400)
                self._reply(status, json.dumps({"detail": str(exc)}).encode())
                return
            self._reply(200, out.model_dump_json().encode())

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            self._handle("POST")

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@pytest.fixture
def platform() -> Iterator[tuple[Coordinator, str]]:
    rng = random.Random(4)
    coordinator = Coordinator(
        LaneSettings(mode="shadow", replicas=1),
        clock=lambda: 1_763_000_000.0,
        rng=rng,
        nonce=rng.randbytes,
    )
    for obj in synthetic_objects(5, seed=8):
        coordinator.ingest(obj.data, obj.media_type, obj.label)
    server = _server(coordinator)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield coordinator, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def test_a_node_enrolls_fills_and_acks_over_http(
    platform: tuple[Coordinator, str],
) -> None:
    coordinator, url = platform
    key = DeviceKey.generate()
    node = EdgeNode(
        transport=HttpTransport(api_url=url, key=key, clock=lambda: 1_763_000_000.0),
        store=MemoryStore(),
        config=NodeConfig(
            public_key_hex=key.public_key_hex,
            quota_bytes=GIB,
            client=ClientInfo(name="http-test", version="1"),
        ),
    )
    assert node.enroll().node_id == key.node_id
    report = node.step()
    assert len(report.filled) == 5
    assert node.store.digests() == set(coordinator.objects)
    assert all(a.acked_at is not None for a in coordinator.assignments.values())
    assert node.transport.status().held_objects == 5


def test_http_errors_surface_with_their_status(
    platform: tuple[Coordinator, str],
) -> None:
    _, url = platform
    transport = HttpTransport(
        api_url=url, key=DeviceKey.generate(), clock=lambda: 1_763_000_000.0
    )
    with pytest.raises(TransportError) as raised:
        transport.poll(PollRequest(protocol="edge/v1", free_bytes=0))
    assert raised.value.status == 401


@pytest.mark.parametrize(
    "url",
    [
        "http://platform.example.test",
        "http://127.0.0.1.attacker.example",
        "http://localhostly.example",
        "ftp://127.0.0.1",
        "https://",
    ],
)
def test_plain_http_is_refused_off_loopback(url: str) -> None:
    with pytest.raises(ValueError):
        HttpTransport(api_url=url, key=DeviceKey.generate())


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"]
)
def test_loopback_http_is_allowed(url: str) -> None:
    HttpTransport(api_url=url, key=DeviceKey.generate())


def test_https_is_always_allowed() -> None:
    HttpTransport(api_url="https://platform-api.heyditto.ai", key=DeviceKey.generate())
