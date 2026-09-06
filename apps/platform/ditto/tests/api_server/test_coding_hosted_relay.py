from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import struct
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from ditto.api_models.coding_hosted_relay import HostedRelayBinding
from ditto.api_models.coding_inference import coding_inference_canonical_json_bytes
from ditto.api_server.coding_hosted_provider import HostedProviderAdapter
from ditto.api_server.coding_hosted_relay import MAGIC, HostedRelayBridge
from ditto.db.models import CodingHostedInferenceGrant, CodingHostedInferenceRequest
from ditto.tests.api_server.test_coding_hosted_inference import (
    ROOT,
    fixture,
    locked_body,
)
from ditto.tests.api_server.test_coding_hosted_provider import (
    SyntheticEstimator,
    http_response,
    response_body,
)

TOKEN = bytes(range(32))


def miner_body():
    value = json.loads(locked_body())
    for key in ("n", "stream", "store", "usage", "provider"):
        value.pop(key)
    value["reasoning"].pop("exclude")
    return json.dumps(value).encode()


async def exchange(path, binding, operation, body=b"", *, token=TOKEN, request_id=None):
    reader, writer = await asyncio.open_unix_connection(path)
    value = {
        "schema": "dittobench-coding-hosted-relay-command-v2",
        "binding_sha256": binding.digest(),
        "operation": operation,
        "request_id": str(request_id or uuid4()),
        "miner_request_base64": base64.b64encode(body).decode(),
    }
    encoded = coding_inference_canonical_json_bytes(value)
    writer.write(MAGIC + token + struct.pack(">I", len(encoded)) + encoded)
    await writer.drain()
    try:
        async with asyncio.timeout(10):
            size = struct.unpack(">I", await reader.readexactly(4))[0]
            result = json.loads(await reader.readexactly(size))
            assert await reader.read() == b""
            return result
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.fixture
async def bridge_setup(session_maker):
    authority, worker, policy, _, ledger, grant = await fixture(session_maker)
    calls, retained = [], []
    gate = asyncio.Event()
    gate.set()

    async def provider(request):
        calls.append(json.loads(request.content))
        await gate.wait()
        return http_response(response_body())

    async def retain(result):
        retained.append(result)

    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=SyntheticEstimator(),
        api_key="synthetic",
        _test_transport=httpx.MockTransport(provider),
    )
    async with session_maker() as session:
        row = await session.get(CodingHostedInferenceGrant, grant)
        expiry = int(row.expires_at.timestamp())
    binding = HostedRelayBinding(
        schema="dittobench-coding-hosted-relay-binding-v2",
        evaluation_id=authority.evaluation_id,
        attempt_id=authority.attempt_id,
        worker_id=worker,
        grant_id=grant,
        assignment_sha256=authority.digest(),
        policy_sha256=policy.digest(),
        artifact_sha256=authority.artifact_sha256,
        harness_instance_id="synthetic-container",
        profile_capability_id="hosted-profile",
        expires_at_unix=expiry,
    )
    bridge = HostedRelayBridge(
        adapter=adapter, binding=binding, token=TOKEN, retain_evidence=retain
    )
    with TemporaryDirectory(prefix="hosted-relay-") as directory:
        path = Path(directory) / "worker.sock"
        server = await bridge.start(path)
        try:
            yield path, binding, bridge, ledger, calls, retained, gate
        finally:
            server.close()
            await server.wait_closed()
            await bridge.revoke()


async def test_native_bridge_locks_request_and_retains_private_evidence(bridge_setup):
    path, binding, _, ledger, calls, retained, _ = bridge_setup
    await exchange(path, binding, "open")
    result = await exchange(path, binding, "complete", miner_body())
    assert len(calls) == len(retained) == 1
    assert calls[0]["provider"]["only"] == ["azure/eu"]
    assert calls[0]["store"] is False and calls[0]["reasoning"]["exclude"] is True
    assert "provider_evidence" not in result
    assert "private_provider_debug" in json.loads(retained[0].provider_evidence)
    assert result["settlement"]["grant_id"] == str(binding.grant_id)
    assert (await exchange(path, binding, "revoke"))["ledger_drained"] is True
    assert (await ledger.accounting(binding.grant_id)).verified


@pytest.mark.parametrize(
    "mode", ["token", "binding", "not_open", "open_twice", "expired"]
)
async def test_bridge_refuses_wrong_or_reused_authority(bridge_setup, mode):
    path, binding, _, _, calls, _, _ = bridge_setup
    token = TOKEN
    operation = "open"
    if mode == "token":
        token = b"z" * 32
    elif mode == "binding":
        binding = binding.model_copy(update={"worker_id": uuid4()})
    elif mode == "not_open":
        operation = "complete"
    elif mode == "open_twice":
        await exchange(path, binding, "open")
    else:
        # Mismatched expiry is a different authority even with the same token.
        binding = binding.model_copy(update={"expires_at_unix": 1})
    with pytest.raises(asyncio.IncompleteReadError):
        await exchange(
            path,
            binding,
            operation,
            miner_body() if operation == "complete" else b"",
            token=token,
        )
    assert not calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", {}),
        ("stream", True),
        ("model", "other"),
        ("reasoning", {"effort": "high"}),
        ("max_completion_tokens", True),
    ],
)
async def test_miner_cannot_steer_provider_or_policy(bridge_setup, field, value):
    path, binding, _, _, calls, _, _ = bridge_setup
    await exchange(path, binding, "open")
    body = json.loads(miner_body())
    body[field] = value
    with pytest.raises(asyncio.IncompleteReadError):
        await exchange(path, binding, "complete", json.dumps(body).encode())
    assert not calls


async def test_same_request_identity_never_dispatches_twice(bridge_setup):
    path, binding, _, _, calls, _, _ = bridge_setup
    await exchange(path, binding, "open")
    request_id = uuid4()
    await exchange(path, binding, "complete", miner_body(), request_id=request_id)
    with pytest.raises(asyncio.IncompleteReadError):
        await exchange(path, binding, "complete", miner_body(), request_id=request_id)
    assert len(calls) == 1


async def test_disconnect_cancels_provider_but_cannot_clear_pending_charge(
    bridge_setup,
):
    path, binding, _, ledger, calls, _, gate = bridge_setup
    gate.clear()
    await exchange(path, binding, "open")
    operation = asyncio.create_task(exchange(path, binding, "complete", miner_body()))
    async with asyncio.timeout(5):
        while not calls:
            await asyncio.sleep(0.01)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    result = await exchange(path, binding, "revoke")
    assert result["ledger_drained"] is False
    state = await ledger.accounting(binding.grant_id)
    assert state.pending_count == 1 and state.cost_usd_micros is None


async def test_real_go_source_relay_to_python_provider_and_postgres(
    bridge_setup, session_maker
):
    path, binding, _, ledger, calls, retained, _ = bridge_setup
    go = shutil.which("go")
    assert go is not None, "Go is required for the native relay integration"
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "GOCACHE", "GOMODCACHE")
        if name in os.environ
    }
    environment.update(
        DITTO_HOSTED_RELAY_TEST_SOCKET=str(path),
        DITTO_HOSTED_RELAY_TEST_BINDING=binding.model_dump_json(by_alias=True),
        DITTO_HOSTED_RELAY_TEST_TOKEN=TOKEN.hex(),
        DITTO_HOSTED_RELAY_TEST_BODY=base64.b64encode(miner_body()).decode(),
    )
    child = await asyncio.create_subprocess_exec(
        go,
        "test",
        "-p",
        "2",
        "-count=1",
        "-run",
        "^TestPrivateBridgeIntegration$",
        "./internal/codinghostedrelay",
        cwd=ROOT / "services/dittobench-api",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(180):
            output, _ = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert child.returncode == 0, output.decode()
    assert len(calls) == len(retained) == 1
    assert (await ledger.accounting(binding.grant_id)).verified
    async with session_maker() as session:
        rows = (await session.scalars(select(CodingHostedInferenceRequest))).all()
        assert len(rows) == 1 and rows[0].state == "settled"


async def test_control_cancellation_does_not_interrupt_provider_cleanup(bridge_setup):
    path, binding, bridge, ledger, _, _, _ = bridge_setup
    retaining, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_retention(_result):
        retaining.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    bridge._retain = slow_retention
    await exchange(path, binding, "open")
    pending = asyncio.create_task(exchange(path, binding, "complete", miner_body()))
    await asyncio.wait_for(retaining.wait(), 5)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await bridge.revoke()
        assert cleaning.is_set()
        assert bridge._active is not None and not bridge._active.done()
        assert bridge._active.cancelling() == 1
        assert not (await ledger.accounting(binding.grant_id)).verified
    finally:
        release.set()
    with pytest.raises(asyncio.IncompleteReadError):
        await pending
    assert await bridge.revoke()


async def test_missing_evidence_never_releases_output(bridge_setup):
    path, binding, bridge, _, calls, _, _ = bridge_setup

    async def broken_retention(_result):
        raise OSError("synthetic private evidence details")

    bridge._retain = broken_retention
    await exchange(path, binding, "open")
    with pytest.raises(asyncio.IncompleteReadError):
        await exchange(path, binding, "complete", miner_body())
    assert len(calls) == 1
    # This is ONLY ledger drain, not successful output or scoreable evidence.
    assert await bridge.revoke()


@pytest.mark.parametrize(
    "field", ["artifact_sha256", "assignment_sha256", "worker_id", "expires_at_unix"]
)
async def test_binding_must_match_live_database_authority(bridge_setup, field):
    path, binding, bridge, _, calls, _, _ = bridge_setup
    changed = uuid4() if field == "worker_id" else "f" * 64
    if field == "expires_at_unix":
        changed = binding.expires_at_unix + 1
    binding = binding.model_copy(update={field: changed})
    bridge._binding = binding  # Simulate a misconfigured trusted worker.
    with pytest.raises(asyncio.IncompleteReadError):
        await exchange(path, binding, "open")
    assert not calls
