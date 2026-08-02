#!/usr/bin/env python3
"""Content-addressed cache for deterministic Ollama embedding requests."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODEL = "embeddinggemma"
MAX_BODY = 2 * 1024 * 1024


class Cache:
    def __init__(self, path: str, upstreams: list[str]):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, body BLOB NOT NULL)")
        self.db.commit()
        self.lock = threading.Lock()
        self.inflight: dict[str, threading.Event] = {}
        self.upstreams = itertools.cycle(upstreams)
        self.hits = self.misses = self.waits = 0

    def get(self, key: str) -> bytes | None:
        with self.lock:
            row = self.db.execute("SELECT body FROM embeddings WHERE key = ?", (key,)).fetchone()
            if row:
                self.hits += 1
                return bytes(row[0])
            return None

    def resolve(self, key: str, request_body: bytes) -> bytes:
        cached = self.get(key)
        if cached is not None:
            return cached
        with self.lock:
            event = self.inflight.get(key)
            if event is None:
                event = self.inflight[key] = threading.Event()
                owner = True
                upstream = next(self.upstreams)
                self.misses += 1
            else:
                owner = False
                upstream = ""
                self.waits += 1
        if not owner:
            event.wait(timeout=300)
            cached = self.get(key)
            if cached is None:
                raise RuntimeError("coalesced embedding request failed")
            return cached
        try:
            request = urllib.request.Request(
                upstream.rstrip("/") + "/api/embed",
                data=request_body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                body = response.read()
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO embeddings(key, body) VALUES (?, ?)", (key, body))
                self.db.commit()
            return body
        finally:
            with self.lock:
                self.inflight.pop(key, None)
                event.set()

    def stats(self) -> dict[str, int]:
        with self.lock:
            entries = self.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            return {"entries": entries, "hits": self.hits, "misses": self.misses, "coalesced_waits": self.waits}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def send_json(self, status: int, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.send_json(HTTPStatus.OK, {"status": "ok", "model": MODEL, **self.server.cache.stats()})  # type: ignore[attr-defined]

    def do_POST(self) -> None:
        if self.path != "/api/embed":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid body size"})
            return
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("model") not in {MODEL, MODEL + ":latest"}:
                raise ValueError("only embeddinggemma is allowed")
            if not isinstance(value.get("input"), list) or not all(isinstance(x, str) for x in value["input"]):
                raise ValueError("input must be an array of strings")
            canonical = json.dumps({"model": MODEL, "input": value["input"]}, separators=(",", ":"), ensure_ascii=False).encode()
            body = self.server.cache.resolve(hashlib.sha256(canonical).hexdigest(), canonical)  # type: ignore[attr-defined]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})


def main() -> None:
    upstreams = [x.strip() for x in os.environ.get("OLLAMA_UPSTREAMS", "").split(",") if x.strip()]
    if not upstreams:
        raise SystemExit("OLLAMA_UPSTREAMS is required")
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "11434"))), Handler)
    server.cache = Cache(os.environ.get("CACHE_DB", "/cache/embeddings.sqlite"), upstreams)  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
