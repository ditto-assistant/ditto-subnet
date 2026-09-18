import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ditto_screener.conversation_relay import BUDGET, EMBED_MODEL, MODEL, Relay


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
    with pytest.raises(ValueError):
        Relay.payload("/v1/chat/completions", chat(max_tokens=10**9))


def provider(monkeypatch, body):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append(request)
            assert timeout == 110
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    return calls


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
