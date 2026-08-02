"""Tiny readiness server so a MIG can autoheal broken fleet instances.

The screener is a pull worker with no served traffic, so there was no signal a
regional MIG could use to tell "bootstrapped and running" from "RUNNING but
first-boot bootstrap died (apt / Secret Manager / clone / updater failure)". A
broken-but-RUNNING instance would otherwise be counted as drained capacity.

This exposes one endpoint, ``GET /healthz``, that returns 200 only once the
worker has entered its sweep loop and 503 (or a closed port, if the process
never started) otherwise. The infra health check (terraform/.../screener-fleet.tf)
probes it and autohealing recreates instances that never turn ready. It binds a
plain port with no auth because the only ingress that reaches it is the GCP
health-check range, allowed by a dedicated firewall on fleet-tagged instances.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class ReadinessServer:
    """Serves ``GET /healthz`` -> 200 when ready, 503 until then."""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self._ready = threading.Event()
        ready = self._ready

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                if self.path.rstrip("/") in ("/healthz", ""):
                    code = 200 if ready.is_set() else 503
                    body = b"ready\n" if ready.is_set() else b"starting\n"
                else:
                    code, body = 404, b"not found\n"
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                # Health checks hit this every ~30s; don't spam the worker log.
                return

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="readiness-server",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "readiness server listening on :%d (reporting starting)",
            self._server.server_address[1],
        )

    def set_ready(self) -> None:
        self._ready.set()
        logger.info("readiness server now reporting ready")

    def stop(self) -> None:
        self._ready.clear()
        self._server.shutdown()
        self._server.server_close()
