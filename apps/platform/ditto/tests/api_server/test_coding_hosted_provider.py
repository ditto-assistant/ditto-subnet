from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ditto.api_server.coding_hosted_inference import ReservationCeilings
from ditto.api_server.coding_hosted_provider import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    HostedProviderAdapter,
    HostedProviderError,
)
from ditto.db.models import CodingHostedInferenceRequest
from ditto.tests.api_server.test_coding_hosted_inference import fixture, locked_body
from ditto.tests.db.queries.test_coding_hosted_private import _freeze


class SyntheticEstimator:
    """Fixture only; explicitly not a production token/pricing estimator."""

    def ceilings(self, canonical_request):
        assert canonical_request.endswith(b"\n")
        assert json.loads(canonical_request)["max_completion_tokens"] == 100
        return ReservationCeilings(100, 100, 100)


class Body(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), 8192):
            yield self.body[offset : offset + 8192]

    async def aclose(self):
        self.closed = True


def response_body():
    return {
        "id": "gen-native-" + str(uuid4()),
        "model": "openai/gpt-5.6-luna",
        "provider": "Azure",
        "choices": [{"message": {"role": "assistant", "content": "private answer"}}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
            "cost": 0.0000101,
        },
        "openrouter_metadata": {
            "requested": "openai/gpt-5.6-luna",
            "strategy": "direct",
            "attempt": 1,
            "pipeline": [],
            "is_byok": False,
            "region": "test-edge-not-provider-region",
            "summary": "advisory",
            "future_advisory_field": "ignored",
            "endpoints": {
                "total": 1,
                "available": [
                    {
                        "provider": "Azure",
                        "model": "openai/gpt-5.6-luna",
                        "selected": True,
                    }
                ],
            },
            "attempts": [
                {"provider": "Azure", "model": "openai/gpt-5.6-luna", "status": 200}
            ],
        },
        "private_provider_debug": "must not reach miner or database",
    }


def http_response(payload, *, status=200, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.Response(
        status,
        headers=headers or {"content-type": "application/json"},
        stream=Body(body),
    )


async def setup(maker, handler, **policy_changes):
    authority, worker, policy, _, ledger, grant = await fixture(maker, **policy_changes)
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=SyntheticEstimator(),
        api_key="synthetic-private-key",
        _test_transport=httpx.MockTransport(handler),
    )
    return adapter, authority, worker, ledger, grant


async def test_native_dispatch_commits_before_send_and_settles_before_return(
    session_maker,
):
    calls = []
    payload = response_body()

    async def handler(request):
        calls.append(request)
        async with session_maker() as session:
            rows = (await session.scalars(select(CodingHostedInferenceRequest))).all()
            assert len(rows) == 1 and rows[0].state == "reserved"
        return http_response(payload)

    adapter, authority, worker, ledger, grant = await setup(session_maker, handler)
    value = await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert len(calls) == 1 and str(calls[0].url) == ENDPOINT
    assert calls[0].headers["authorization"] == "Bearer synthetic-private-key"
    assert calls[0].headers["x-openrouter-metadata"] == "enabled"
    assert calls[0].headers["x-openrouter-cache"] == "false"
    assert calls[0].headers.get("cookie") is None
    assert "synthetic-private-key" not in repr(adapter)
    assert "private answer" not in repr(value)
    assert json.loads(value.provider_evidence) == payload
    projection = json.loads(value.response)
    assert projection["schema"] == "dittobench-coding-hosted-inference-response-v2"
    assert (
        "openrouter_metadata" not in projection
        and "private_provider_debug" not in projection
    )
    assert projection["usage"]["cost_usd_micros"] == 11
    assert (
        value.settlement.response_sha256 == hashlib.sha256(value.response).hexdigest()
    )
    assert await adapter.revoke()
    accounting = await ledger.accounting(grant)
    assert accounting.verified and accounting.cost_usd_micros == 11
    assert (await _freeze(session_maker, authority, worker)).newly_frozen


@pytest.mark.parametrize(
    "failure",
    [
        "missing_metadata",
        "fallback",
        "attempt_bool",
        "pipeline",
        "byok",
        "wrong_model",
        "wrong_provider",
        "wrong_total",
        "wrong_selected",
        "extra_attempt",
        "missing_usage",
        "bad_usage",
        "over_prompt",
        "over_cost",
        "string_cost",
        "role",
        "choice_error",
        "duplicate_key",
        "invalid_json",
        "oversize",
        "redirect",
        "http_error",
        "compressed",
        "html",
        "disconnect",
    ],
)
async def test_unverified_response_never_releases_or_clears_reservation(
    session_maker, failure
):
    calls = []
    payload = response_body()
    metadata = payload["openrouter_metadata"]
    mutations = {
        "missing_metadata": lambda: payload.pop("openrouter_metadata"),
        "fallback": lambda: metadata.update(attempt=2),
        "attempt_bool": lambda: metadata.update(attempt=True),
        "pipeline": lambda: metadata.update(pipeline=[{"type": "compression"}]),
        "byok": lambda: metadata.update(is_byok=True),
        "wrong_model": lambda: payload.update(model="another-model"),
        "wrong_provider": lambda: payload.update(provider="another-provider"),
        "wrong_total": lambda: metadata["endpoints"].update(total=True),
        "wrong_selected": lambda: metadata["endpoints"]["available"][0].update(
            selected=1
        ),
        "extra_attempt": lambda: metadata["attempts"].append(metadata["attempts"][0]),
        "missing_usage": lambda: payload.pop("usage"),
        "bad_usage": lambda: payload["usage"].update(total_tokens=31),
        "over_prompt": lambda: payload["usage"].update(
            prompt_tokens=101, total_tokens=111
        ),
        "over_cost": lambda: payload["usage"].update(cost=0.000101),
        "string_cost": lambda: payload["usage"].update(cost="0.00001"),
        "role": lambda: payload["choices"][0]["message"].update(role="system"),
        "choice_error": lambda: payload["choices"][0].update(error={"text": "private"}),
    }
    if failure in mutations:
        mutations[failure]()

    async def handler(request):
        calls.append(request)
        if failure == "disconnect":
            raise httpx.ReadError("synthetic-private-key private provider text")
        if failure == "redirect":
            return http_response(
                b"", status=307, headers={"location": "https://evil.invalid"}
            )
        if failure == "http_error":
            return http_response(payload, status=503)
        if failure == "compressed":
            return http_response(b"compressed", headers={"content-encoding": "gzip"})
        if failure == "html":
            return http_response(b"private", headers={"content-type": "text/html"})
        if failure == "oversize":
            return http_response(b"x" * (MAX_RESPONSE_BYTES + 1))
        if failure == "duplicate_key":
            return http_response(b'{"id":"a","id":"b"}')
        if failure == "invalid_json":
            return http_response(b"not-json-private")
        return http_response(payload)

    adapter, authority, worker, ledger, grant = await setup(session_maker, handler)
    with pytest.raises(HostedProviderError) as error:
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert str(error.value) == "hosted provider attempt did not complete"
    assert not await adapter.revoke()
    assert len(calls) == 1
    status = await ledger.accounting(grant)
    assert (
        status.pending_count == 1
        and not status.verified
        and status.cost_usd_micros is None
    )
    with pytest.raises(IntegrityError):
        await _freeze(session_maker, authority, worker)
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert len(calls) == 1


async def test_provider_generation_cannot_settle_twice_even_with_changed_response(
    session_maker,
):
    payload = response_body()
    adapter, _, _, ledger, grant = await setup(
        session_maker, lambda _r: http_response(payload)
    )
    await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    payload["choices"][0]["message"]["content"] = "changed response"
    payload["usage"]["cost"] = 0.00002
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert not await adapter.revoke()
    assert (await ledger.accounting(grant)).pending_count == 1


async def test_replayed_request_is_not_dispatched(session_maker):
    calls = []

    def handler(request):
        calls.append(request)
        return http_response(response_body())

    adapter, _, _, _, _ = await setup(session_maker, handler)
    request_id = uuid4()
    await adapter.complete(request_id=request_id, locked_request=locked_body())
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=request_id, locked_request=locked_body())
    assert len(calls) == 1


@pytest.mark.parametrize("action", ["cancel", "revoke", "timeout"])
async def test_cancel_or_revocation_never_returns_late_output(session_maker, action):
    started, finish, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_request):
        started.set()
        try:
            await finish.wait()
            return http_response(response_body())
        finally:
            closed.set()

    adapter, _, _, ledger, grant = await setup(
        session_maker, handler, request_timeout_milliseconds=1000
    )
    running = asyncio.create_task(
        adapter.complete(request_id=uuid4(), locked_request=locked_body())
    )
    await asyncio.wait_for(started.wait(), 5)
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    if action == "cancel":
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    elif action == "revoke":
        # External revocation must also suppress a late, otherwise valid reply.
        assert not await ledger.revoke(grant)
        finish.set()
        with pytest.raises(HostedProviderError):
            await running
    else:
        with pytest.raises(HostedProviderError):
            await asyncio.wait_for(running, 5)
    assert closed.is_set()
    await adapter.revoke()
    status = await ledger.accounting(grant)
    assert status.pending_count == (0 if action == "revoke" else 1)


async def test_invalid_policy_request_never_sends(session_maker):
    calls = []
    adapter, _, _, _, _ = await setup(session_maker, lambda r: calls.append(r))
    request = json.loads(locked_body())
    request["provider"]["allow_fallbacks"] = True
    with pytest.raises(HostedProviderError):
        await adapter.complete(
            request_id=uuid4(), locked_request=json.dumps(request).encode()
        )
    assert calls == []


@pytest.mark.parametrize("stage", ["reserve", "settle"])
async def test_lost_commit_acknowledgement_never_retries(
    session_maker, monkeypatch, stage
):
    calls = []

    def handler(request):
        calls.append(request)
        return http_response(response_body())

    adapter, _, _, ledger, grant = await setup(session_maker, handler)
    original = getattr(ledger, stage)

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("private database details")

    monkeypatch.setattr(ledger, stage, lost_ack)
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    await adapter.revoke()
    status = await ledger.accounting(grant)
    assert len(calls) == (0 if stage == "reserve" else 1)
    assert status.pending_count == (1 if stage == "reserve" else 0)
    assert status.verified is (stage == "settle")


async def test_adapter_policy_must_match_committed_grant(session_maker):
    calls = []
    _, _, policy, _, ledger, grant = await fixture(session_maker)
    policy = policy.model_copy(update={"request_timeout_milliseconds": 1000})
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=SyntheticEstimator(),
        api_key="synthetic",
        _test_transport=httpx.MockTransport(lambda r: calls.append(r)),
    )
    with pytest.raises(HostedProviderError):
        await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert calls == []
    assert not await adapter.revoke()


async def test_outbound_client_has_no_ambient_authority(session_maker, monkeypatch):
    settings = []
    payload = response_body()

    def transport(**kwargs):
        settings.append(kwargs)
        return httpx.MockTransport(lambda _r: http_response(payload))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy.invalid:99")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/untrusted-cert")
    _, _, policy, _, ledger, grant = await fixture(session_maker)
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=SyntheticEstimator(),
        api_key="synthetic",
    )
    await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert settings == [{"retries": 0, "trust_env": False}]
    assert await adapter.revoke()


async def test_billing_keeps_decimal_precision_before_rounding_up(session_maker):
    body = (
        json.dumps(response_body())
        .encode()
        .replace(b'"cost": 1.01e-05', b'"cost": 0.00001000000000000000000000000000001')
    )
    assert b"0.00001000000000000000000000000000001" in body
    adapter, _, _, _, _ = await setup(session_maker, lambda _r: http_response(body))
    result = await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    assert result.settlement.cost_usd_micros == 11
    assert await adapter.revoke()
