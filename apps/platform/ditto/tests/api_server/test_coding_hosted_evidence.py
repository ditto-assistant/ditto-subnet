from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from ditto.api_models.coding_hosted_evidence import HostedEvidenceIdentity
from ditto.api_models.coding_hosted_relay import HostedRelayBinding
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_evidence import (
    AiobotoHippiusSealedEvidenceTransport,
    HippiusSealedEvidenceConflict,
    HippiusSealedEvidenceError,
    HippiusSealedEvidenceNotFound,
)
from ditto.api_server.coding_hippius_probe import (
    load_hippius_probe_receipt,
    write_hippius_probe_receipt,
)
from ditto.api_server.coding_hosted_evidence import (
    HostedInferenceEvidencePublisher,
    _aad,
    checked_blob,
)
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.api_server.coding_hosted_provider import HostedProviderAdapter
from ditto.api_server.coding_hosted_relay import HostedRelayBridge
from ditto.db.models import (
    CodingHostedEvidenceFinalization,
    CodingHostedEvidenceReservation,
)
from ditto.tests.api_server.test_coding_hippius_evidence import _config, _probe
from ditto.tests.api_server.test_coding_hosted_budget import profiled_fixture
from ditto.tests.api_server.test_coding_hosted_inference import locked_body
from ditto.tests.api_server.test_coding_hosted_provider import (
    http_response,
    response_body,
)
from ditto.tests.api_server.test_coding_hosted_relay import exchange, miner_body


class Transport:
    def __init__(self, sessions):
        self.sessions = sessions
        self.objects = {}
        self.puts = 0
        self.ambiguous = False
        self.corrupt = False
        self.readback_gate = None

    async def get_object(self, *, key, max_bytes):
        async with self.sessions() as session:
            assert (
                await session.scalars(select(CodingHostedEvidenceReservation))
            ).all()
        if key not in self.objects:
            raise HippiusSealedEvidenceNotFound("synthetic missing")
        value = self.objects[key]
        if self.readback_gate is not None:
            await self.readback_gate.wait()
        assert len(value) <= max_bytes
        return value[:-1] if self.corrupt else value

    async def put_object(self, *, key, body, metadata):
        assert metadata == {}  # No semantic storage metadata.
        self.puts += 1
        self.objects[key] = body
        if self.ambiguous:
            raise OSError("synthetic provider acknowledgement lost")


@pytest.fixture
async def evidence(session_maker, tmp_path):
    _, worker, policy, _, ledger, grant, estimator = await profiled_fixture(
        session_maker
    )
    adapter = HostedProviderAdapter(
        ledger=ledger,
        grant_id=grant,
        policy=policy,
        estimator=estimator,
        api_key="synthetic",
        _test_transport=httpx.MockTransport(lambda _r: http_response(response_body())),
    )
    result = await adapter.complete(request_id=uuid4(), locked_request=locked_body())
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = tmp_path / "public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    public.chmod(0o600)
    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    spool = HostedEvidenceSpool(root, max_bytes=1 << 28, max_objects=10)
    config = _config()
    probe, _ = load_hippius_probe_receipt(_probe(tmp_path, config))
    fresh = tmp_path / "fresh-probe.json"
    write_hippius_probe_receipt(
        receipt=replace(
            probe, checked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ),
        output=fresh,
    )
    transport = Transport(session_maker)
    transport.adapter = adapter
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(public)
    args = {
        "sessions": session_maker,
        "worker_id": worker,
        "spool": spool,
        "wrapper": wrapper,
        "runtime_profile": estimator.canonical_profile(),
        "config": config,
        "probe_receipt_path": fresh,
        "_test_transport": transport,
    }
    publisher = HostedInferenceEvidencePublisher(**args)
    try:
        yield publisher, result, spool, transport, private, args, ledger, grant, root
    finally:
        spool.close()


async def test_native_ciphertext_roundtrip_and_append_only_finalization(
    evidence, session_maker
):
    publisher, result, spool, transport, private, _, ledger, grant, root = evidence
    await publisher(result)
    manifest, sealed = spool.load(result.settlement.request_id)
    identity = HostedEvidenceIdentity.model_validate_json(manifest)
    nonce, wrapped, ciphertext = checked_blob(identity, sealed)
    aad = _aad(identity.model_dump(mode="json", by_alias=True))
    key = private.decrypt(
        wrapped,
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=hashlib.sha256(aad).digest(),
        ),
    )
    plain = AESGCM(key).decrypt(nonce, ciphertext, aad)
    payload = json.loads(plain)
    assert base64.b64decode(payload["response_base64"]) == result.response
    assert (
        base64.b64decode(payload["provider_evidence_base64"])
        == result.provider_evidence
    )
    assert hashlib.sha256(plain).hexdigest() == identity.plaintext_sha256
    assert (
        b"private answer" not in sealed and b"provider_evidence_base64" not in manifest
    )
    assert all(
        (p.stat().st_mode & 0o777) == 0o400
        for p in (root / identity.request_id.hex).iterdir()
    )
    first = await publisher.resume(identity.request_id)
    await publisher(result)
    assert await publisher.resume(identity.request_id) == first
    assert transport.puts == 1
    await ledger.revoke(grant)
    assert len(await publisher.require_complete(grant)) == 64
    for model in (CodingHostedEvidenceReservation, CodingHostedEvidenceFinalization):
        async with session_maker() as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(model))
            await session.rollback()
    async with session_maker() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(CodingHostedEvidenceReservation).values(identity_sha256="f" * 64)
            )


async def test_ambiguous_upload_replays_same_ciphertext_after_restart(
    evidence, monkeypatch
):
    publisher, result, spool, transport, _, args, ledger, grant, root = evidence
    transport.ambiguous = True
    with pytest.raises(HostedEvidenceError):
        await publisher(result)
    original = spool.load(result.settlement.request_id)
    await ledger.revoke(grant)
    with pytest.raises(HostedEvidenceError):
        await publisher.require_complete(grant)
    spool.close()
    reopened = HostedEvidenceSpool(root, max_bytes=1 << 28, max_objects=10)

    rotated = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = root.parent / "rotated.pem"
    public.write_bytes(
        rotated.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    public.chmod(0o600)
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(public)

    async def forbidden(**_kwargs):
        raise AssertionError("replay must not wrap a new key")

    monkeypatch.setattr(wrapper, "wrap_data_key", forbidden)
    transport.ambiguous = False
    try:
        resumed = HostedInferenceEvidencePublisher(
            **{**args, "spool": reopened, "wrapper": wrapper}
        )
        receipt = await resumed.resume(result.settlement.request_id)
        assert receipt.ciphertext_sha256 == hashlib.sha256(original[1]).hexdigest()
        assert reopened.load(result.settlement.request_id) == original
        assert transport.puts == 1
        assert len(await resumed.require_complete(grant)) == 64
    finally:
        reopened.close()


async def test_conflicting_readback_never_finalizes(evidence, session_maker):
    publisher, result, _, transport, _, _, _, _, _ = evidence
    transport.corrupt = True
    with pytest.raises(HostedEvidenceError):
        await publisher(result)
    async with session_maker() as session:
        assert not (
            await session.scalars(select(CodingHostedEvidenceFinalization))
        ).all()
    transport.corrupt = False
    key = next(iter(transport.objects))
    transport.objects[key] = b"x" * len(transport.objects[key])
    with pytest.raises(HostedEvidenceError):
        await publisher.resume(result.settlement.request_id)
    assert transport.puts == 1


async def test_different_raw_evidence_or_worker_is_rejected(evidence):
    publisher, result, _, transport, _, args, _, _, _ = evidence
    other = HostedInferenceEvidencePublisher(**{**args, "worker_id": uuid4()})
    with pytest.raises(HostedEvidenceError):
        await other(result)
    assert not transport.objects
    await publisher(result)
    raw = json.loads(result.provider_evidence)
    raw["advisory"] = "different bytes with same normalized result"
    with pytest.raises(HostedEvidenceError):
        await publisher(replace(result, provider_evidence=json.dumps(raw).encode()))
    assert transport.puts == 1


async def test_store_failure_never_contacts_provider(evidence, monkeypatch):
    publisher, result, _, transport, _, _, _, _, _ = evidence

    def failed(*_args, **_kwargs):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(HostedEvidenceError):
        await publisher(result)
    assert transport.puts == 0 and not transport.objects


async def test_duplicate_callbacks_preserve_one_identity(evidence):
    publisher, result, spool, _, _, _, _, _, _ = evidence
    await asyncio.gather(publisher(result), publisher(result))
    identity = HostedEvidenceIdentity.model_validate_json(
        spool.load(result.settlement.request_id)[0]
    )
    assert (
        await publisher.resume(result.settlement.request_id)
    ).identity_sha256 == identity.digest()


async def test_cancelled_readback_replays_without_new_inference(
    evidence, session_maker
):
    publisher, result, spool, transport, _, _, ledger, grant, _ = evidence
    transport.readback_gate = asyncio.Event()
    pending = asyncio.create_task(publisher(result))
    try:
        async with asyncio.timeout(10):
            while not transport.puts:
                await asyncio.sleep(0.01)
        original = spool.load(result.settlement.request_id)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with session_maker() as session:
            assert not (
                await session.scalars(select(CodingHostedEvidenceFinalization))
            ).all()
        await ledger.revoke(grant)
        with pytest.raises(HostedEvidenceError):
            await publisher.require_complete(grant)
        transport.readback_gate.set()
        await publisher.resume(result.settlement.request_id)
        assert spool.load(result.settlement.request_id) == original
        assert transport.puts == 1
        assert len(await publisher.require_complete(grant)) == 64
    finally:
        transport.readback_gate.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_probe_expiry_during_readback_blocks_finalization(
    evidence, session_maker
):
    publisher, result, _, transport, _, _, _, _, _ = evidence
    transport.readback_gate = asyncio.Event()
    pending = asyncio.create_task(publisher(result))
    try:
        async with asyncio.timeout(10):
            while not transport.puts:
                await asyncio.sleep(0.01)
        publisher._probe = replace(
            publisher._probe,
            checked_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        transport.readback_gate.set()
        with pytest.raises(HostedEvidenceError):
            await pending
        async with session_maker() as session:
            assert not (
                await session.scalars(select(CodingHostedEvidenceFinalization))
            ).all()
        assert transport.puts == 1
    finally:
        transport.readback_gate.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def test_spool_rejects_symlinks_locks_and_incomplete_entries(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    spool = HostedEvidenceSpool(root, max_bytes=100000, max_objects=2)
    try:
        with pytest.raises(BlockingIOError):
            HostedEvidenceSpool(root, max_bytes=100000, max_objects=2)
        alias = tmp_path / "alias"
        alias.symlink_to(root)
        with pytest.raises(OSError):
            HostedEvidenceSpool(alias, max_bytes=100000, max_objects=2)
    finally:
        spool.close()
    (root / uuid4().hex).mkdir(mode=0o700)
    with pytest.raises(HostedEvidenceError):
        HostedEvidenceSpool(root, max_bytes=100000, max_objects=2)


async def test_spool_capacity_and_tampering_fail_closed(evidence):
    publisher, result, spool, transport, _, args, _, _, root = evidence
    await publisher(result)
    path = root / result.settlement.request_id.hex / "sealed.bin"
    path.chmod(0o600)
    with pytest.raises(HostedEvidenceError):
        await publisher.resume(result.settlement.request_id)
    assert transport.puts == 1
    path.chmod(0o400)
    spool.close()
    limited = HostedEvidenceSpool(root, max_bytes=1 << 28, max_objects=1)
    try:
        with pytest.raises(HostedEvidenceError):
            limited.store(uuid4(), b"identity", b"ciphertext")
    finally:
        limited.close()
    # Config changes cannot redirect an existing reserved object to a new bucket.
    config = _config(bucket="other-evidence")
    with pytest.raises(HostedEvidenceError):
        HostedInferenceEvidencePublisher(**{**args, "config": config})


async def test_bridge_withholds_model_output_until_remote_evidence_finalizes(evidence):
    publisher, result, _, transport, _, args, _, _, _ = evidence
    await publisher(result)
    source = await publisher._snapshot(result.settlement.request_id)
    from ditto.db.models import CodingHostedAssignment

    async with args["sessions"]() as session:
        assignment = await session.get(
            CodingHostedAssignment, result.settlement.evaluation_id
        )
    binding = HostedRelayBinding(
        schema="dittobench-coding-hosted-relay-binding-v2",
        evaluation_id=result.settlement.evaluation_id,
        attempt_id=result.settlement.attempt_id,
        worker_id=args["worker_id"],
        grant_id=result.settlement.grant_id,
        assignment_sha256=source.fields["assignment_sha256"],
        policy_sha256=source.policy.digest(),
        artifact_sha256=assignment.artifact_sha256,
        harness_instance_id="native-evidence-test",
        profile_capability_id="hosted-profile",
        expires_at_unix=source.reservation.expires_at_unix,
    )
    bridge = HostedRelayBridge(
        adapter=transport.adapter,
        binding=binding,
        token=bytes(range(32)),
        retain_evidence=publisher,
    )
    transport.readback_gate = asyncio.Event()
    with TemporaryDirectory(prefix="native-evidence-") as directory:
        from pathlib import Path

        path = Path(directory) / "bridge.sock"
        server = await bridge.start(path)
        try:
            await exchange(path, binding, "open")
            pending = asyncio.create_task(
                exchange(path, binding, "complete", miner_body())
            )
            async with asyncio.timeout(10):
                while transport.puts < 2:
                    await asyncio.sleep(0.01)
            assert not pending.done()
            async with args["sessions"]() as session:
                assert (
                    len(
                        (
                            await session.scalars(
                                select(CodingHostedEvidenceFinalization)
                            )
                        ).all()
                    )
                    == 1
                )
            transport.readback_gate.set()
            response = await pending
            assert "provider_evidence" not in response
            assert (await exchange(path, binding, "revoke"))["ledger_drained"] is True
            assert (
                len(await publisher.require_complete(result.settlement.grant_id)) == 64
            )
        finally:
            transport.readback_gate.set()
            await bridge.revoke()
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize(
    "url",
    [
        "https://wrong.hippius.com/coding-sealed-evidence/object.bin",
        "http://s3.hippius.com/coding-sealed-evidence/object.bin",
        "https://s3.hippius.com/another-bucket/object.bin",
        "https://s3.hippius.com/coding-sealed-evidence/other.bin",
        "https://user@s3.hippius.com/coding-sealed-evidence/object.bin",
        "https://s3.hippius.com/coding-sealed-evidence/object.bin?redirect=1",
        "https://s3.hippius.com/coding-sealed-evidence/object.bin;different-object",
    ],
)
def test_sdk_target_drift_is_rejected_before_send(url):
    transport = AiobotoHippiusSealedEvidenceTransport(_config())
    with pytest.raises(HippiusSealedEvidenceConflict):
        transport._check_outbound(SimpleNamespace(url=url), key="object.bin")
    transport._check_outbound(
        SimpleNamespace(url="https://s3.hippius.com/coding-sealed-evidence/object.bin"),
        key="object.bin",
    )


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_sdk_http_client_does_not_follow_redirects(method):
    from botocore.awsrequest import AWSRequest

    seen = []

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            seen.append(head.split(b"\r\n", 1)[0])
            if b"/original " in seen[-1]:
                writer.write(
                    b"HTTP/1.1 307 Temporary Redirect\r\n"
                    b"Location: /escaped\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    # Exercise the configured SDK HTTP implementation against loopback only.
    # Production requests also pass the separate exact-HTTPS-target hook.
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        transport = AiobotoHippiusSealedEvidenceTransport(_config())
        async with transport._client() as client:
            request = AWSRequest(
                method=method, url=f"http://127.0.0.1:{port}/original", data=b"sealed"
            ).prepare()
            response = await client._endpoint.http_session.send(request)
            assert response.status_code == 307
        assert seen == [f"{method} /original HTTP/1.1".encode()]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_sdk_checks_mutated_target_before_network_send(method, monkeypatch):
    from aiobotocore.httpsession import AIOHTTPSession

    sent = []

    async def forbidden_send(_self, request):
        sent.append(request.url)
        raise AssertionError("mutated request must never reach HTTP transport")

    def redirect(request, **_kwargs):
        request.url = "https://wrong.hippius.com/coding-sealed-evidence/object.bin"

    monkeypatch.setattr(AIOHTTPSession, "send", forbidden_send)
    transport = AiobotoHippiusSealedEvidenceTransport(_config())
    transport._session.events.register_first("before-send.s3", redirect)
    with pytest.raises(HippiusSealedEvidenceError):
        if method == "GET":
            await transport.get_object(key="object.bin", max_bytes=100)
        else:
            await transport.put_object(key="object.bin", body=b"sealed", metadata={})
    assert not sent
