"""The body cap refuses oversized requests before the application parses them."""

from __future__ import annotations

import json
import logging

import pytest

from ditto.api_server.middleware.request_body_limit import (
    RequestBodyLimitMiddleware,
    request_body_limit_bytes,
)


def _scope(*headers: tuple[bytes, bytes]) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/upload/agent",
        "headers": list(headers),
        "query_string": b"",
    }


async def _invoke(app, scope, messages):
    sent: list[dict] = []
    index = 0

    async def receive():
        nonlocal index
        if index >= len(messages):
            return {"type": "http.disconnect"}
        message = messages[index]
        index += 1
        return message

    async def send(message):
        sent.append(message)

    await RequestBodyLimitMiddleware(app, max_bytes=4)(scope, receive, send)
    return sent


def test_limit_covers_a_transcript_and_a_raised_tarball() -> None:
    assert request_body_limit_bytes(20 * 1024 * 1024) == 32 << 20
    assert request_body_limit_bytes((32 << 20) + 1) == (32 << 20) + 1 + (1 << 20)


async def test_declared_length_over_the_cap_never_reaches_the_app() -> None:
    called = False

    async def app(_scope, _receive, _send):
        nonlocal called
        called = True

    sent = await _invoke(
        app,
        _scope((b"content-length", b"100")),
        [{"type": "http.request", "body": b"x" * 100, "more_body": False}],
    )

    assert called is False
    assert _rejection(sent)["error_code"] == 3002
    assert _rejection(sent)["message"] == "request body is too large"
    assert _rejection(sent)["request_id"] == "-"


async def test_a_body_at_the_cap_is_delivered_intact() -> None:
    seen = b""

    async def app(_scope, receive, send):
        nonlocal seen
        message = await receive()
        seen = message["body"]
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    sent = await _invoke(
        app,
        _scope((b"content-length", b"4")),
        [{"type": "http.request", "body": b"abcd", "more_body": False}],
    )

    assert seen == b"abcd"
    assert sent[0]["status"] == 204


async def test_a_chunked_body_stops_at_the_cap() -> None:
    seen = b""

    async def app(_scope, receive, send):
        nonlocal seen
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            seen += message.get("body", b"")
            if not message.get("more_body", False):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 204,
                        "headers": [],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return

    sent = await _invoke(
        app,
        _scope(),
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )

    assert seen == b"abc"
    assert _rejection(sent)["error_code"] == 3002


async def test_a_post_rejection_error_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(_scope, receive, _send):
        await receive()
        message = await receive()
        assert message["type"] == "http.disconnect"
        raise RuntimeError("handler failed after cutoff")

    with caplog.at_level(logging.ERROR):
        sent = await _invoke(
            app,
            _scope(),
            [
                {"type": "http.request", "body": b"abc", "more_body": True},
                {"type": "http.request", "body": b"def", "more_body": False},
            ],
        )

    assert _rejection(sent)["error_code"] == 3002
    assert "handler failed after cutoff" in caplog.text


def _rejection(sent: list[dict]) -> dict:
    assert sent[0]["status"] == 413
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


def test_a_bool_tarball_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive int"):
        request_body_limit_bytes(True)  # type: ignore[arg-type]
