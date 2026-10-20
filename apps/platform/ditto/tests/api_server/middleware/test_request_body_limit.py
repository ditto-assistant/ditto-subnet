"""RequestBodyLimitMiddleware: declared and streamed caps, route overrides."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Request, UploadFile
from pydantic import BaseModel
from starlette.types import Message

from ditto.api_server import create_api_server
from ditto.api_server.endpoints.upload import MAX_TARBALL_SIZE_BYTES
from ditto.api_server.middleware import (
    REQUEST_ID_HEADER,
    RequestBodyLimitMiddleware,
    RequestIDMiddleware,
    register_exception_handlers,
)
from ditto.api_server.middleware.error_envelope import ERROR_CODE_REQUEST_TOO_LARGE
from ditto.tests.api_server.conftest import make_api_server_config

_DEFAULT = 1024
_UPLOAD = 4096


class _Payload(BaseModel):
    text: str


def _build() -> FastAPI:
    app = FastAPI()
    app.state.calls = []

    @app.post("/api/v1/model")
    async def model(payload: _Payload, request: Request) -> dict:
        request.app.state.calls.append("model")
        return {"length": len(payload.text)}

    @app.post("/api/v1/raw")
    async def raw(request: Request) -> dict:
        request.app.state.calls.append("raw")
        return {"length": len(await request.body())}

    @app.post("/api/v1/upload/agent")
    async def upload(
        request: Request, agent_tar: Annotated[UploadFile, File()]
    ) -> dict:
        request.app.state.calls.append("upload")
        return {"length": len(await agent_tar.read())}

    register_exception_handlers(app)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        default_max_bytes=_DEFAULT,
        route_max_bytes=[(r"/api/v1/upload/agent", _UPLOAD)],
    )
    app.add_middleware(RequestIDMiddleware)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _chunks(total: int, size: int = 256) -> AsyncIterator[bytes]:
    for start in range(0, total, size):
        yield b" " * min(size, total - start)


def _assert_refused(response: httpx.Response, limit: int) -> None:
    assert response.status_code == 413
    body = response.json()
    assert body["error_code"] == ERROR_CODE_REQUEST_TOO_LARGE
    assert body["message"] == f"request body exceeds {limit} bytes"
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


class TestRequestBodyLimit:
    async def test_body_under_the_cap_reaches_the_endpoint(self) -> None:
        app = _build()
        async with _client(app) as client:
            response = await client.post("/api/v1/model", json={"text": "x" * 512})
        assert response.status_code == 200
        assert response.json() == {"length": 512}
        assert app.state.calls == ["model"]

    async def test_declared_length_over_the_cap_is_refused_unread(self) -> None:
        reads = 0

        async def endpoint(*_: object) -> None:
            raise AssertionError("the app must not run")

        async def receive() -> Message:
            nonlocal reads
            reads += 1
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        middleware = RequestBodyLimitMiddleware(endpoint, default_max_bytes=_DEFAULT)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/raw",
            "headers": [(b"content-length", str(_DEFAULT + 1).encode())],
        }
        await middleware(scope, receive, send)

        assert reads == 0
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 413

    async def test_declared_length_refusal_carries_the_envelope(self) -> None:
        app = _build()
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/model", json={"text": "x" * (_DEFAULT + 1)}
            )
        _assert_refused(response, _DEFAULT)
        assert app.state.calls == []

    async def test_chunked_body_over_the_cap_never_reaches_a_model_handler(
        self,
    ) -> None:
        app = _build()
        async with _client(app) as client:
            response = await client.post(
                "/api/v1/model",
                content=_chunks(_DEFAULT * 4),
                headers={"Content-Type": "application/json"},
            )
        assert "content-length" not in response.request.headers
        _assert_refused(response, _DEFAULT)
        assert app.state.calls == []

    async def test_chunked_body_over_the_cap_fails_a_handler_reading_it(
        self,
    ) -> None:
        app = _build()
        async with _client(app) as client:
            response = await client.post("/api/v1/raw", content=_chunks(_DEFAULT + 1))
        _assert_refused(response, _DEFAULT)
        assert app.state.calls == ["raw"]

    async def test_chunked_body_at_the_cap_passes(self) -> None:
        app = _build()
        async with _client(app) as client:
            response = await client.post("/api/v1/raw", content=_chunks(_DEFAULT))
        assert response.status_code == 200
        assert response.json() == {"length": _DEFAULT}

    async def test_upload_route_has_its_own_cap(self) -> None:
        app = _build()
        files = {"agent_tar": ("a.tar.gz", b"x" * (_DEFAULT * 2))}
        async with _client(app) as client:
            ok = await client.post("/api/v1/upload/agent", files=files)
            assert ok.status_code == 200
            assert ok.json() == {"length": _DEFAULT * 2}
            refused = await client.post("/api/v1/raw", content=b"x" * (_DEFAULT * 2))
            _assert_refused(refused, _DEFAULT)
        assert app.state.calls == ["upload"]

    async def test_oversized_multipart_is_refused_before_the_handler(self) -> None:
        app = _build()
        files = {"agent_tar": ("a.tar.gz", b"x" * _UPLOAD)}
        request = httpx.Request("POST", "http://test/api/v1/upload/agent", files=files)
        body = request.read()

        async def stream() -> AsyncIterator[bytes]:
            for start in range(0, len(body), 512):
                yield body[start : start + 512]

        async with _client(app) as client:
            declared = await client.post("/api/v1/upload/agent", files=files)
            chunked = await client.post(
                "/api/v1/upload/agent",
                content=stream(),
                headers={"Content-Type": request.headers["Content-Type"]},
            )
        _assert_refused(declared, _UPLOAD)
        _assert_refused(chunked, _UPLOAD)
        assert app.state.calls == []


class TestFactoryWiring:
    def test_limit_sits_just_inside_the_request_id(self) -> None:
        app = create_api_server(make_api_server_config())
        classes = [m.cls for m in app.user_middleware]
        assert classes[:2] == [RequestIDMiddleware, RequestBodyLimitMiddleware]

    def test_route_caps_leave_the_endpoint_checks_reachable(self) -> None:
        config = make_api_server_config()
        app = create_api_server(config)
        entry = next(
            m for m in app.user_middleware if m.cls is RequestBodyLimitMiddleware
        )
        kwargs: dict[str, Any] = dict(entry.kwargs)
        limiter = RequestBodyLimitMiddleware(app, **kwargs)
        assert limiter.max_bytes_for("/api/v1/upload/agent") > MAX_TARBALL_SIZE_BYTES
        assert limiter.max_bytes_for("/api/v1/validator/heartbeat") == 16 * 1024
        assert limiter.max_bytes_for("/api/v1/screener/heartbeat") == 4096
        assert limiter.max_bytes_for("/api/v1/validator/agent/a/transcript/run-1") == (
            32 << 20
        )
        assert limiter.max_bytes_for("/api/v1/validator/job") == (
            config.request_body_max_bytes
        )

    async def test_oversized_heartbeat_is_refused_before_auth(self) -> None:
        app = create_api_server(make_api_server_config())
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            response = await c.post(
                "/api/v1/validator/heartbeat", content=_chunks(16 * 1024 + 1)
            )
        _assert_refused(response, 16 * 1024)
