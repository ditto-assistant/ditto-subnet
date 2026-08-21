"""HTTP surface for preview-control cheatcodes."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from ditto.preview.align import align_engine
from ditto.preview.engine import IsolationError, PreviewEngine


class _Ignore(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RegisterBody(_Ignore):
    hotkey: str
    permit: bool = False
    stake: float = 0.0


class PermitBody(_Ignore):
    hotkey: str
    enabled: bool = True


class WarpBody(_Ignore):
    n: int = 1


class LeaseBody(_Ignore):
    hotkey: str | None = None
    lease_id: str | None = None
    lifetime_blocks: int = 100


class GrantBody(_Ignore):
    grant_id: str | None = None


class ProviderBody(_Ignore):
    status: int | None = None


class DropBody(_Ignore):
    dropped: bool = True


class SnapshotBody(_Ignore):
    name: str


class AlignBody(_Ignore):
    hotkeys: list[str] = Field(default_factory=list)
    json_path: str | None = None
    database_url: str | None = None


def make_handler(engine: PreviewEngine) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closed over ``engine``."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in {"/health", "/v1/health"}:
                self._send(200, {"ok": True, "network": engine.network})
                return
            if path == "/v1/state":
                self._send(200, engine.state())
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._json()
                payload = self._dispatch(path, body)
            except IsolationError as exc:
                self._send(403, {"error": str(exc)})
                return
            except (KeyError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
                return
            except FileNotFoundError as exc:
                self._send(404, {"error": str(exc)})
                return
            if payload is None:
                self._send(404, {"error": "not found"})
                return
            self._send(200, payload)

        def _dispatch(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
            if path == "/v1/cheat/register":
                req = RegisterBody.model_validate(body)
                neuron = engine.register(req.hotkey, permit=req.permit, stake=req.stake)
                return {
                    "hotkey": neuron.hotkey,
                    "uid": neuron.uid,
                    "permit": neuron.permit,
                }
            if path == "/v1/cheat/permit":
                req = PermitBody.model_validate(body)
                neuron = engine.permit(req.hotkey, req.enabled)
                return {"hotkey": neuron.hotkey, "permit": neuron.permit}
            if path == "/v1/cheat/warp_block":
                req = WarpBody.model_validate(body)
                return {"block": engine.warp_block(req.n)}
            if path == "/v1/cheat/warp_tempo":
                req = WarpBody.model_validate(body)
                return {"block": engine.warp_tempo(req.n)}
            if path == "/v1/cheat/issue_lease":
                req = LeaseBody.model_validate(body)
                if not req.hotkey:
                    raise ValueError("hotkey is required")
                lease = engine.issue_lease(
                    req.hotkey, lifetime_blocks=req.lifetime_blocks
                )
                return {
                    "lease_id": lease.lease_id,
                    "expires_at_block": lease.expires_at_block,
                }
            if path == "/v1/cheat/expire_lease":
                req = LeaseBody.model_validate(body)
                return {"expired": engine.expire_lease(req.lease_id)}
            if path == "/v1/cheat/issue_grant":
                grant = engine.issue_grant()
                return {"grant_id": grant.grant_id}
            if path == "/v1/cheat/exhaust_allowance":
                req = GrantBody.model_validate(body)
                return {"exhausted": engine.exhaust_allowance(req.grant_id)}
            if path == "/v1/cheat/inject_provider":
                req = ProviderBody.model_validate(body)
                engine.inject_provider(req.status)
                return {"provider_status": engine.provider_status}
            if path == "/v1/cheat/drop_relay":
                req = DropBody.model_validate(body)
                engine.drop_relay(req.dropped)
                return {"relay_dropped": engine.relay_dropped}
            if path == "/v1/cheat/snapshot":
                req = SnapshotBody.model_validate(body)
                engine.snapshot(req.name)
                return {"snapshot": req.name}
            if path == "/v1/cheat/revert":
                req = SnapshotBody.model_validate(body)
                engine.revert(req.name)
                return {"reverted": req.name, "block": engine.block}
            if path == "/v1/cheat/align_from_db":
                req = AlignBody.model_validate(body)
                aligned = align_engine(
                    engine,
                    hotkeys=req.hotkeys or None,
                    json_path=Path(req.json_path) if req.json_path else None,
                    database_url=req.database_url,
                )
                return {"aligned": aligned}
            return None

        def _json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("JSON object required")
            return parsed

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            blob = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

    return Handler


class PreviewServer:
    """Threading HTTP server for one :class:`PreviewEngine`."""

    def __init__(self, engine: PreviewEngine, host: str = "127.0.0.1", port: int = 0):
        self.engine = engine
        self._httpd = ThreadingHTTPServer((host, port), make_handler(engine))
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> str:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def serve_forever(
    engine: PreviewEngine,
    *,
    host: str = "127.0.0.1",
    port: int = 4077,
) -> None:
    """Block serving cheatcodes on ``host:port``."""
    httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
