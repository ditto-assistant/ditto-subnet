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
    assert payload["provider"]["max_price"] == {"prompt": 0.1, "completion": 0.4}
    # The bench's aggregate route: fall back across providers on a rate limit,
    # never onto the excluded 4-bit endpoint, always under the price ceiling.
    assert payload["provider"]["allow_fallbacks"] is True
    assert payload["provider"]["ignore"] == ["coreweave"]
    assert payload["provider"]["zdr"] is True
    assert "only" not in payload["provider"]
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
            assert timeout == 240
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    return calls


def test_private_slice_rejects_paid_dispatch_before_provider_call(
    tmp_path, monkeypatch
):
    calls = provider(monkeypatch, {"usage": {"cost": 0}})
    relay = Relay("secret", tmp_path / "usage.json", budget_microusd=1)
    with pytest.raises(RelayError, match="inference_budget_exhausted"):
        relay.post("/v1/chat/completions", chat())
    assert calls == []
    assert relay.requests == 0 and relay.spent == 0


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
        {"usage": {"prompt_tokens": 20, "completion_tokens": 8192, "cost": 0.0025}},
    )
    relay = Relay("secret", tmp_path / "usage.json")
    relay.post(route, body)
    assert json.loads(calls[0].data)[field] == 8192
    assert relay.tokens == 8212 and relay.spent == 2500 and not relay.failed
    assert relay.chat_dispatches == relay.successful_chat_responses == 1


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
    # 10 tokens at the $0.10/M ceiling, rounded up to a whole microUSD.
    assert relay.spent == 1 and relay.cost_is_upper_bound
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
    sleeps = []

    class Opener:
        def open(self, request, **_kwargs):
            calls.append(request)
            raise HTTPError("https://secret.example", 429, "secret body", {}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    monkeypatch.setattr("time.sleep", sleeps.append)
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
    # Rate limits are retried (unbilled), then the first status is kept.
    assert len(calls) == 4 and usage.provider_retries == 3
    assert sleeps == [4.0, 8.0, 16.0]
    assert "secret" not in relay.state_file.read_text()


@pytest.mark.parametrize("status", [429, 502, 503])
def test_transient_provider_error_is_retried_then_metered(
    tmp_path, monkeypatch, status
):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "cost": 0.00004,
                    },
                }
            ).encode()

    class Opener:
        def open(self, request, **_kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError("https://x", status, "busy", {"Retry-After": "1"}, None)
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    monkeypatch.setattr("time.sleep", lambda _s: None)
    relay = Relay("secret", tmp_path / "usage.json")
    relay.post("/v1/chat/completions", chat())
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert len(calls) == 2 and usage.provider_retries == 1
    assert not usage.failed and not usage.unmetered and usage.spent_microusd == 40


def test_timeouts_are_never_retried(tmp_path, monkeypatch):
    calls = []

    class Opener:
        def open(self, request, **_kwargs):
            calls.append(request)
            raise TimeoutError

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    relay = Relay("secret", tmp_path / "usage.json")
    with pytest.raises(TimeoutError):
        relay.post("/v1/chat/completions", chat())
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert len(calls) == 1 and usage.failed and usage.unmetered
    assert usage.failure.code == "timeout"


def test_dispatches_overlap_up_to_the_slot_limit(tmp_path, monkeypatch):
    import threading

    active, peak, lock = [0], [0], threading.Lock()
    full = threading.Event()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0}
            return json.dumps({"choices": [], "usage": usage}).encode()

    class Opener:
        def open(self, _request, **_kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                if active[0] == 4:
                    full.set()
            # Hold the call until four dispatches are inside the provider at
            # once; a serial relay would time out here with a peak of one.
            assert full.wait(timeout=10), "dispatches never overlapped"
            with lock:
                active[0] -= 1
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    relay = Relay("secret", tmp_path / "usage.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: relay.post("/v1/chat/completions", chat()), range(8)))
    assert peak[0] == 4  # Overlapping, and never past the slot limit.
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert usage.requests == 8 and not usage.unmetered and not usage.failed


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
    assert relay.spent == 6 and relay.cost_is_upper_bound
    assert not relay.unmetered and not relay.failed


def test_harness_ingress_is_fixed_target_and_response_bounded(monkeypatch):
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout == 300
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


def test_retries_stop_inside_one_harness_operation(tmp_path, monkeypatch):
    """A slow 429 near the operation deadline is not retried past it."""
    calls = []
    clock = [1_000.0]

    class Opener:
        def open(self, _request, *, timeout):
            calls.append(timeout)
            clock[0] += 260  # The provider took most of the operation.
            raise HTTPError("https://x", 429, "busy", {"Retry-After": "20"}, None)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("must not back off"))
    relay = Relay("secret", tmp_path / "usage.json")
    with pytest.raises(HTTPError):
        relay.post("/v1/chat/completions", chat())
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert calls == [240] and usage.provider_retries == 0
    assert usage.failure.http_status == 429


def test_nan_retry_after_falls_back_to_bounded_backoff(tmp_path, monkeypatch):
    sleeps = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
                }
            ).encode()

    class Opener:
        calls = 0

        def open(self, _request, **_kwargs):
            Opener.calls += 1
            if Opener.calls == 1:
                raise HTTPError("https://x", 503, "busy", {"Retry-After": "NaN"}, None)
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    monkeypatch.setattr("time.sleep", sleeps.append)
    relay = Relay("secret", tmp_path / "usage.json")
    relay.post("/v1/chat/completions", chat())
    assert sleeps == [4.0] and Opener.calls == 2 and not relay.failed


def test_slot_wait_spends_the_caller_deadline_and_refunds(tmp_path, monkeypatch):
    calls = provider(monkeypatch, {"choices": [], "usage": {"cost": 0}})
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    relay = Relay("secret", tmp_path / "usage.json")

    class Busy:
        def acquire(self, timeout):
            assert 0 < timeout <= 270
            clock[0] += timeout  # Every slot stayed busy for the whole wait.
            return False

        def release(self):
            pytest.fail("a slot that was never acquired is never released")

    relay.slots = Busy()
    with pytest.raises(RelayError, match="relay_busy"):
        relay.post("/v1/chat/completions", chat())
    usage = HarnessUsage.model_validate_json(relay.state_file.read_text())
    assert not calls and usage.spent_microusd == 0 and not usage.unmetered
    assert usage.failure.code == "relay_busy"
    raw = json.loads(relay.state_file.read_text())
    assert raw["requests"] == 0 and raw["chat_dispatches"] == 0


def test_budget_waits_for_in_flight_holds_then_exhausts_distinctly(
    tmp_path, monkeypatch
):
    import threading

    started, release = threading.Event(), threading.Event()
    calls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Opener:
        def open(self, request, **_kwargs):
            calls.append(request)
            if len(calls) == 1:
                started.set()
                assert release.wait(timeout=10)
            usage = {"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.00001}
            return Response(json.dumps({"choices": [], "usage": usage}).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    # One dispatch reserves ~2.6k microUSD at the ceiling: two cannot be held
    # at once inside 4k, but each settles at 10 microUSD.
    relay = Relay("secret", tmp_path / "usage.json", budget_microusd=4_000)
    first = threading.Thread(target=lambda: relay.post("/v1/chat/completions", chat()))
    first.start()
    assert started.wait(timeout=10)
    second_done = threading.Event()

    def second():
        relay.post("/v1/chat/completions", chat())
        second_done.set()

    waiter = threading.Thread(target=second)
    waiter.start()
    assert not second_done.wait(timeout=0.3)  # Waiting, not failed.
    release.set()
    first.join(timeout=10)
    waiter.join(timeout=10)
    assert second_done.is_set() and len(calls) == 2 and relay.spent == 20
    assert not relay.failed
    relay.spent = 2_000  # Less than one hold left, with nothing in flight.
    with pytest.raises(RelayError, match="inference_budget_exhausted"):
        relay.post("/v1/chat/completions", chat())
    assert len(calls) == 2


def test_ceiling_prices_cover_every_listed_provider():
    from ditto_screener.conversation_relay import CHAT_PROVIDER, ceiling_microusd

    # Highest gpt-oss-20b list price on 2026-11-08: Groq $0.075 in / $0.30 out.
    assert CHAT_PROVIDER["max_price"] == {"prompt": 0.1, "completion": 0.4}
    assert ceiling_microusd(1_000_000, 0) == 100_000
    assert ceiling_microusd(0, 1_000_000) == 400_000


def test_waiter_out_of_time_with_holds_in_flight_is_not_exhaustion(
    tmp_path, monkeypatch
):
    relay = Relay("secret", tmp_path / "usage.json", budget_microusd=4_000)
    relay.in_flight, relay.spent = 1, 3_000  # Another dispatch holds the rest.
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])

    def wait(**_kwargs):
        clock[0] += 400  # The holder never settles before our deadline.
        return False

    relay.settled_cv.wait = wait
    with pytest.raises(RelayError, match="^inference_budget_unavailable$"):
        relay.post("/v1/chat/completions", chat())


def test_a_failure_wakes_budget_waiters(tmp_path, monkeypatch):
    import threading

    provider(monkeypatch, {"choices": [], "usage": {"cost": 0}})
    relay = Relay("secret", tmp_path / "usage.json", budget_microusd=4_000)
    relay.in_flight, relay.spent = 1, 3_000
    outcome = []

    def waiter():
        try:
            relay.post("/v1/chat/completions", chat())
        except RelayError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=waiter)
    thread.start()
    import time as clock

    clock.sleep(0.2)
    with relay.lock:
        relay.fail(RelayError("unsupported_message_content"), "request")
    thread.join(timeout=5)
    assert not thread.is_alive() and outcome == ["inference_budget_unavailable"]
