import contextlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError

import pytest

from ditto_screener.conversation_relay import (
    BUDGET,
    EMBED_MODEL,
    MODEL,
    Relay,
    RelayError,
    harness_request,
    harness_stdio,
)
from ditto_screening_protocol.conversation import HarnessUsage


def chat(**kwargs):
    return {
        "messages": [{"role": "user", "content": "Remember the blue notebook."}],
        **kwargs,
    }


def test_route_and_model_override_cannot_escape_budget_profile():
    route, payload, embed = Relay.payload(
        "/v1/chat/completions",
        chat(
            model="expensive",
            provider={"only": ["evil"]},
            route="evil",
            plugins=[{"id": "web"}],
        ),
    )
    assert route == "chat/completions" and not embed
    assert payload["model"] == MODEL
    assert payload["provider"]["max_price"] == {"prompt": 2, "completion": 2}
    assert payload["provider"]["allow_fallbacks"] is False
    assert "plugins" not in payload and "route" not in payload
    with pytest.raises(ValueError):
        Relay.payload("/proxy/https://evil.example", chat())
    _, bounded, _ = Relay.payload("/v1/chat/completions", chat(max_tokens=10**9))
    assert bounded["max_tokens"] == 8192


def provider(monkeypatch, body):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append(request)
            assert timeout == 110
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    return calls


@pytest.mark.parametrize(
    "route,body,field",
    [
        ("/v1/chat/completions", chat(max_tokens=16000), "max_tokens"),
        ("/v1/chat/completions", chat(max_completion_tokens=16000), "max_tokens"),
        (
            "/v1/responses",
            {"input": "Hello", "max_output_tokens": 16000},
            "max_output_tokens",
        ),
    ],
)
def test_larger_client_allowance_uses_unchanged_ceiling(
    tmp_path, monkeypatch, route, body, field
):
    calls = provider(
        monkeypatch,
        {"usage": {"prompt_tokens": 20, "completion_tokens": 8192, "cost": 0.016424}},
    )
    relay = Relay("secret", tmp_path / "usage.json")
    relay.post(route, body)
    assert json.loads(calls[0].data)[field] == 8192
    assert relay.tokens == 8212 and relay.spent == 16424 and not relay.failed


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192", None])
def test_invalid_output_limit_remains_rejected(value):
    with pytest.raises(ValueError):
        Relay.payload("/v1/chat/completions", chat(max_tokens=value))
    with pytest.raises(ValueError):
        Relay.payload("/v1/responses", {"input": "Hello", "max_output_tokens": value})


def test_ambiguous_provider_usage_is_reserved_and_never_replayed(tmp_path, monkeypatch):
    calls = provider(monkeypatch, {"usage": {"prompt_tokens": 1}})
    relay = Relay("secret", tmp_path / "usage.json")
    with pytest.raises(ValueError):
        relay.post("/v1/chat/completions", chat())
    reserved = relay.spent
    with pytest.raises(ValueError):
        relay.post("/v1/chat/completions", chat())
    assert len(calls) == 1 and 0 < reserved <= BUDGET
    assert relay.spent == reserved and relay.failed and relay.unmetered
    assert "secret" not in (tmp_path / "usage.json").read_text()


def test_embedding_without_dollars_is_a_labelled_cost_bound(tmp_path, monkeypatch):
    provider(
        monkeypatch,
        {
            "data": [{"index": 0, "embedding": [0.0] * 768}],
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
            "model": EMBED_MODEL,
        },
    )
    relay = Relay("secret", tmp_path / "usage.json")
    result = relay.post("/api/embed", {"input": ["notebook"]})
    assert len(result["embeddings"][0]) == 768
    assert relay.spent == 20 and relay.cost_is_upper_bound
    assert not relay.unmetered and not relay.failed


def test_rig_text_parts_reach_provider_unchanged_and_are_metered(tmp_path, monkeypatch):
    # rig-core 0.38.2 OpenAI Message::System uses OneOrMany<SystemContent>;
    # unlike its user serializer, it emits an array even for one text part.
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "Be helpful."}]},
        {"role": "user", "content": "Remember the blue notebook."},
        {"role": "assistant", "content": None, "tool_calls": []},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": [
                {"type": "text", "text": "blue"},
                {"type": "text", "text": " notebook"},
            ],
        },
    ]
    calls = provider(
        monkeypatch,
        {
            "choices": [{"message": {"content": "Remembered."}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.00004},
        },
    )
    relay = Relay("secret", tmp_path / "usage.json")
    result = relay.post("/v1/chat/completions", {"messages": messages})
    assert json.loads(calls[0].data)["messages"] == messages
    assert result["choices"][0]["message"]["content"] == "Remembered."
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert usage.tokens == 30 and usage.spent_microusd == 40
    assert not usage.failed and usage.failure is None


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "image_url", "image_url": {"url": "https://private.example"}}],
        [{"type": "text", "text": "secret", "image_url": "https://private.example"}],
        [{"type": "text", "text": {"secret": "not text"}}],
        ["secret"],
        42,
    ],
)
def test_unsupported_content_is_private_terminal_and_never_dispatched(
    tmp_path, monkeypatch, content
):
    calls = provider(monkeypatch, {})
    relay = Relay("secret", tmp_path / "usage.json")
    with pytest.raises(ValueError):
        relay.post(
            "/v1/chat/completions",
            chat(messages=[{"role": "user", "content": content}]),
        )
    with pytest.raises(ValueError):
        relay.post("/v1/chat/completions", chat())
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert usage.failed and usage.failure.code == "unsupported_message_content"
    assert usage.failure.stage == "request"
    assert usage.requests == 0 and usage.spent_microusd == 0 and not calls
    assert "secret" not in relay.state_file.read_text()


def test_private_provider_failure_retains_reservation_and_first_status(
    tmp_path, monkeypatch
):
    calls = []

    class Opener:
        def open(self, request, **_kwargs):
            calls.append(request)
            raise HTTPError("https://secret.example", 429, "secret body", {}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    relay = Relay("secret", tmp_path / "usage.json")
    with pytest.raises(HTTPError):
        relay.post("/v1/chat/completions", chat())
    with pytest.raises(ValueError):
        relay.post("/private/secret", {})
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert usage.failure.model_dump() == {
        "code": "provider_http_error",
        "stage": "provider",
        "http_status": 429,
    }
    assert usage.failed and usage.unmetered and usage.spent_microusd > 0
    assert len(calls) == 1 and "secret" not in relay.state_file.read_text()


def test_concurrent_requests_reserve_before_dispatch(tmp_path, monkeypatch):
    calls = provider(
        monkeypatch,
        {
            "choices": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.00004},
        },
    )
    relay = Relay("secret", tmp_path / "usage.json")
    relay.requests = 299

    def attempt(_):
        try:
            relay.post("/v1/chat/completions", chat())
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(results) == 1 and len(calls) == 1 and relay.requests == 300
    assert relay.spent == 40


def test_byok_zero_router_fee_retains_provider_price_bound(tmp_path, monkeypatch):
    provider(
        monkeypatch,
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "cost": 0,
                "is_byok": True,
            },
        },
    )
    relay = Relay("secret", tmp_path / "usage.json")
    relay.post("/v1/chat/completions", chat())
    assert relay.spent == 60 and relay.cost_is_upper_bound
    assert not relay.unmetered and not relay.failed


def test_harness_ingress_is_fixed_target_and_response_bounded(monkeypatch):
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout == 120
            response = io.BytesIO(b'{"pairs":1}')
            response.status = 200
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    assert harness_request("/seed", b'{"pairs":[]}') == (200, b'{"pairs":1}')
    assert requests[0].full_url == "http://agent:8080/seed"
    assert set(requests[0].headers) == {"Content-type"}
    for path, body in [
        ("/v1/chat/completions", b"{}"),
        ("http://evil", None),
        ("/health?url=evil", None),
        ("/run", b"x" * 131073),
    ]:
        with pytest.raises(ValueError):
            harness_request(path, body)
    assert len(requests) == 1

    class LargeOpener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(b"x" * 64001)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: LargeOpener())
    with pytest.raises(ValueError, match="oversized"):
        harness_request("/health", None)


@pytest.mark.parametrize(
    "failure,code",
    [
        (TimeoutError("private body"), "timeout"),
        (URLError(TimeoutError("private URL")), "timeout"),
        (URLError("private hostname"), "connection_failed"),
        (ConnectionResetError("private request"), "connection_failed"),
        (RelayError("oversized_harness_response"), "response_too_large"),
        (ValueError("private JSON"), "transport_failed"),
    ],
)
def test_bridge_failures_are_fixed_codes_without_tracebacks(
    monkeypatch, capsys, failure, code
):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure

    monkeypatch.setattr("ditto_screener.conversation_relay.harness_request", fail)
    monkeypatch.setattr(
        "sys.stdin",
        io.TextIOWrapper(io.BytesIO(b'{"path":"/run","body":"e30=","timeout":120}')),
    )
    harness_stdio()
    output = capsys.readouterr()
    assert json.loads(output.out) == {"error": code}
    assert output.err == "" and "private" not in output.out
    assert len(calls) == 1  # Error reporting never repeats the HTTP operation.


def test_real_socket_deadline_survives_stdio_without_paid_requests(monkeypatch, capsys):
    import threading
    import time
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(self.path)
            time.sleep(1.1)
            self.send_response(200)
            self.end_headers()
            with contextlib.suppress(BrokenPipeError):
                self.wfile.write(b'{"final_text":"private answer"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class LoopbackOnly:
        def open(self, request, *, timeout):
            assert request.full_url == "http://agent:8080/run"
            local = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/run", data=request.data
            )
            return real.open(local, timeout=timeout)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: LoopbackOnly())
    monkeypatch.setattr(
        "sys.stdin",
        io.TextIOWrapper(io.BytesIO(b'{"path":"/run","body":"e30=","timeout":1}')),
    )
    try:
        harness_stdio()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    output = capsys.readouterr()
    assert json.loads(output.out) == {"error": "timeout"}
    assert output.err == "" and "private" not in output.out
    assert calls == ["/run"]
