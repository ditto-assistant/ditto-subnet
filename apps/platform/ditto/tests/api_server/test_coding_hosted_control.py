from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ditto.api_models.coding_hosted_control import (
    AuthoringIdentity,
    RetentionHeader,
    SourceBinding,
)
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_evidence import HippiusSealedEvidenceNotFound
from ditto.api_server.coding_hippius_probe import (
    load_hippius_probe_receipt,
    write_hippius_probe_receipt,
)
from ditto.api_server.coding_hosted_authoring_evidence import (
    HostedAuthoringEvidencePublisher,
    aad,
    canonical,
    check_blob,
    projection,
    sha,
)
from ditto.api_server.coding_hosted_control import HostedAuthoringControl
from ditto.api_server.coding_hosted_control_server import HostedControlServer
from ditto.api_server.coding_hosted_evidence import HostedInferenceEvidencePublisher
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.db.models import (
    CodingHostedAuthoringFinalization,
    CodingHostedAuthoringReservation,
    CodingHostedInferenceGrant,
    CodingHostedInferenceRequest,
    CodingHostedPrivateTask,
)
from ditto.tests.api_server.test_coding_hippius_evidence import _config, _probe
from ditto.tests.api_server.test_coding_hosted_budget import budget_profile
from ditto.tests.api_server.test_coding_hosted_inference import native_policy
from ditto.tests.api_server.test_coding_hosted_inputs import (
    REPO,
    fixture,
)
from ditto.tests.api_server.test_coding_hosted_inputs import (
    hosted_profile as hosted_profile,
)
from ditto.tests.api_server.test_coding_hosted_provider import (
    http_response,
    response_body,
)
from ditto.tests.api_server.test_coding_hosted_relay import miner_body


class Storage:
    def __init__(self, sessions):
        self.sessions = sessions
        self.objects = {}
        self.puts = 0
        self.ambiguous = False
        self.corrupt = False

    async def get_object(self, *, key, max_bytes):
        if key not in self.objects:
            raise HippiusSealedEvidenceNotFound("synthetic missing")
        value = self.objects[key]
        assert len(value) <= max_bytes
        return value[:-1] if self.corrupt else value

    async def put_object(self, *, key, body, metadata):
        assert not metadata
        if key.startswith("coding-hosted-authoring/"):
            async with self.sessions() as session:
                assert (
                    await session.scalars(select(CodingHostedAuthoringReservation))
                ).all()
        assert key not in self.objects
        self.objects[key] = body
        self.puts += 1
        if self.ambiguous:
            raise OSError("synthetic lost acknowledgement")


@pytest.fixture
async def control_fixture(tmp_path, session_maker, hosted_profile):
    hosted_profile = json.loads(json.dumps(hosted_profile))
    hosted_profile["profile"]["budgets"]["wall_time_seconds"] = 300
    hosted_profile["sha256"] = sha(canonical(hosted_profile["profile"]))
    now = int(time.time())
    profile = budget_profile(valid_from_unix=now - 1, valid_until_unix=now + 3600)
    policy = native_policy(runtime_profile_sha256=profile.digest())
    authority, worker, grants, retriever, assembler, reader, unwrapper = await fixture(
        tmp_path, session_maker, hosted_profile, policy_sha256=policy.digest()
    )
    execution = canonical(hosted_profile["profile"])
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = tmp_path / "evidence-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    public.chmod(0o600)
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(public)
    config = _config()
    probe, _ = load_hippius_probe_receipt(_probe(tmp_path, config))
    fresh = tmp_path / "fresh.json"
    write_hippius_probe_receipt(
        receipt=replace(probe, checked_at=datetime.now(UTC).isoformat()), output=fresh
    )
    spools = []
    for name in ("inference-spool", "authoring-spool"):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        spools.append(HostedEvidenceSpool(root, max_bytes=2 << 30, max_objects=1024))
    storage = Storage(session_maker)
    inference = HostedInferenceEvidencePublisher(
        sessions=session_maker,
        worker_id=worker,
        spool=spools[0],
        wrapper=wrapper,
        runtime_profile=profile.canonical_bytes(),
        config=config,
        probe_receipt_path=fresh,
        _test_transport=storage,
    )
    evidence = HostedAuthoringEvidencePublisher(
        sessions=session_maker,
        worker_id=worker,
        spool=spools[1],
        wrapper=wrapper,
        config=config,
        probe_receipt_path=fresh,
        _test_transport=storage,
    )
    bridge_context = tempfile.TemporaryDirectory(prefix="hosted-control-")
    bridges = Path(bridge_context.name) / "bridges"
    bridges.mkdir(mode=0o700)
    control = HostedAuthoringControl(
        sessions=session_maker,
        worker_id=worker,
        assembler=assembler,
        policy=policy,
        execution_profile=execution,
        budget_profile=profile.canonical_bytes(),
        api_key="synthetic-provider-key",
        inference_evidence=inference,
        authoring_evidence=evidence,
        bridge_root=bridges,
        _test_transport=httpx.MockTransport(lambda _r: http_response(response_body())),
    )
    source = SourceBinding(
        evaluation_id=authority.evaluation_id,
        attempt_id=authority.attempt_id,
        worker_id=worker,
        assignment_sha256=authority.digest(),
        artifact_sha256=authority.artifact_sha256,
        harness_instance_id="integration-instance",
        profile_capability_id=f"hosted-{authority.attempt_id}",
        deadline_unix=authority.deadline_unix,
    )
    try:
        yield control, source, authority, grants, retriever, storage, private, spools
    finally:
        for state in control._bridges.values():
            if state.bridge is not None:
                await state.bridge.revoke()
            if state.server is not None:
                state.server.close()
                await state.server.wait_closed()
        for spool in spools:
            spool.close()
        bridge_context.cleanup()


def decrypt_blob(blob, body, private):
    nonce, wrapped, ciphertext = check_blob(blob, body)
    associated = aad(projection(blob))
    key = private.decrypt(
        wrapped,
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=hashlib.sha256(associated).digest(),
        ),
    )
    return AESGCM(key).decrypt(nonce, ciphertext, associated)


async def test_go_worker_runs_connected_platform_adapters(
    control_fixture, session_maker, tmp_path
):
    control, source, authority, grants, retriever, storage, private, spools = (
        control_fixture
    )
    root = control._root.parent / "control"
    root.mkdir(mode=0o700)
    token = bytes(range(32))
    socket = root / "control.sock"
    server = await HostedControlServer(control, token).start(socket)
    description = await retriever.describe_authoring(grants.authoring_grant_id)
    job = await control._assembler._job(
        source.evaluation_id, source.attempt_id, source.assignment_sha256
    )
    expected = {
        "evaluation_id": str(source.evaluation_id),
        "attempt_id": str(source.attempt_id),
        "worker_id": str(source.worker_id),
        "assignment_sha256": source.assignment_sha256,
        "registration_sha256": authority.registration_sha256,
        "execution_profile_sha256": sha(control._execution),
        "task_commitment_sha256": description.task_commitment_sha256,
        "deadline_unix": source.deadline_unix,
        "max_patch_bytes": job.max_patch_bytes,
        "catalog_index": job.catalog_index,
        "corpus_release_id": description.corpus_release_id,
        "private_release_sha256": description.private_release_sha256,
    }
    output = tmp_path / "go-result.json"
    path = tmp_path / "go-config.json"
    path.write_bytes(
        canonical(
            {
                "socket": str(socket),
                "token": base64.b64encode(token).decode(),
                "control": {
                    "Expected": expected,
                    "Profile": json.loads(control._execution),
                },
                "artifact": source.artifact_sha256,
                "agent": str(authority.agent_id),
                "miner_body": base64.b64encode(miner_body()).decode(),
                "output": str(output),
            }
        )
    )
    path.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        "go",
        "test",
        "./internal/codinghostedworker",
        "-run",
        "^TestPlatformControlAdapterIntegration$",
        "-count=1",
        cwd=REPO / "services/dittobench-api",
        env={**os.environ, "DITTO_HOSTED_CONTROL_TEST": str(path)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        server.close()
        await server.wait_closed()
    result = json.loads(output.read_bytes())
    async with session_maker() as session:
        row = await session.get(CodingHostedAuthoringReservation, source.evaluation_id)
        final = await session.get(
            CodingHostedAuthoringFinalization, source.evaluation_id
        )
        task = await session.get(CodingHostedPrivateTask, source.evaluation_id)
        grant = await session.scalar(
            select(CodingHostedInferenceGrant).where(
                CodingHostedInferenceGrant.evaluation_id == source.evaluation_id
            )
        )
        requests = (await session.scalars(select(CodingHostedInferenceRequest))).all()
    assert (
        final is not None
        and task.frozen_patch_sha256 == result["patch_sha256"]
        and task.closed_at is not None
    )
    assert (
        row.identity_sha256 == result["evidence_sha256"]
        and len(requests) == 1
        and requests[0].state == "settled"
    )
    assert int(grant.expires_at.timestamp()) < source.deadline_unix
    identity = AuthoringIdentity.model_validate_json(canonical(row.identity))
    assert identity.inference_evidence_sha256 and identity.header.completed_run
    objects = list(control._evidence._blobs(identity))
    clear = b"".join(decrypt_blob(blob, body, private) for blob, body in objects[:-1])
    freeze = json.loads(clear[: identity.header.freeze_size])
    patch = base64.b64decode(freeze["submission"]["patch"])
    assert sha(patch) == result["patch_sha256"] and b"print(2)" in base64.b64decode(
        json.loads(patch)["changes"][0]["after_content"]
    )
    assert (
        sha(clear[identity.header.freeze_size :]) == identity.header.transcript_sha256
    )
    for cls in (CodingHostedAuthoringReservation, CodingHostedAuthoringFinalization):
        async with session_maker() as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(cls))


async def test_wrong_worker_and_profile_fail_before_retrieval(control_fixture):
    control, source, *_ = control_fixture
    with pytest.raises(HostedEvidenceError):
        await control.check(source.model_copy(update={"worker_id": uuid4()}))
    with pytest.raises(HostedEvidenceError):
        await control.check(source.model_copy(update={"assignment_sha256": "f" * 64}))


async def chunks(body):
    for start in range(0, len(body), 65536):
        yield body[start : start + 65536]


async def test_chunked_ciphertext_recovery_and_conflicts(control_fixture, monkeypatch):
    control, source, _, _, _, storage, private, _ = control_fixture
    # Force multiple chunks with small synthetic bytes without changing public limits.
    monkeypatch.setattr(
        "ditto.api_server.coding_hosted_authoring_evidence.CHUNK_BYTES", 128
    )
    freeze = canonical(
        {"failure": {"kind": "validator_infrastructure", "code": "synthetic"}}
    )
    transcript = b"synthetic transcript\n" * 30
    header = RetentionHeader(
        completed_run=False,
        freeze_sha256=sha(freeze),
        freeze_size=len(freeze),
        transcript_sha256=sha(transcript),
        transcript_size=len(transcript),
    )
    storage.ambiguous = True
    with pytest.raises(HostedEvidenceError):
        await control.retain(source, header, freeze, chunks(transcript))
    original = control._evidence._existing(source.attempt_id)
    storage.ambiguous = False
    digest = await control.retain(source, header, freeze, chunks(transcript))
    assert control._evidence._existing(source.attempt_id) == original
    identity = AuthoringIdentity.model_validate_json(canonical(original[0]))
    assert identity.chunk_count > 1 and identity.digest() == digest
    assert (
        b"".join(
            decrypt_blob(blob, body, private)
            for blob, body in list(control._evidence._blobs(identity))[:-1]
        )
        == freeze + transcript
    )
    count = storage.puts
    assert (
        await control.retain(source, header, freeze, chunks(transcript)) == digest
        and storage.puts == count
    )
    bad = header.model_copy(
        update={
            "transcript_sha256": sha(transcript + b"other"),
            "transcript_size": len(transcript) + 5,
        }
    )
    with pytest.raises(HostedEvidenceError):
        await control.retain(source, bad, freeze, chunks(transcript + b"other"))
    with pytest.raises(HostedEvidenceError):
        await control.freeze(source, b"fake patch", digest)


def failed_evidence():
    transcript = b"one bounded diagnostic event\n"
    freeze = canonical(
        {
            "failure": {
                "kind": "validator_infrastructure",
                "code": "synthetic",
                "authoring_transcript_sha256": sha(transcript),
                "authoring_transcript_bytes": len(transcript),
            }
        }
    )
    header = RetentionHeader(
        completed_run=False,
        freeze_sha256=sha(freeze),
        freeze_size=len(freeze),
        transcript_sha256=sha(transcript),
        transcript_size=len(transcript),
    )
    return header, freeze, transcript


async def test_corrupt_remote_bytes_never_finalize_or_overwrite(
    control_fixture, session_maker
):
    control, source, _, _, _, storage, _, _ = control_fixture
    header, freeze, transcript = failed_evidence()
    storage.corrupt = True
    with pytest.raises(HostedEvidenceError):
        await control.retain(source, header, freeze, chunks(transcript))
    async with session_maker() as session:
        assert (
            await session.get(CodingHostedAuthoringFinalization, source.evaluation_id)
            is None
        )
    key = next(iter(storage.objects))
    original = storage.objects[key]
    storage.corrupt = False
    storage.objects[key] = b"x" * len(original)
    with pytest.raises(HostedEvidenceError):
        await control._evidence.resume(source)
    assert storage.puts == 1
    storage.objects[key] = original
    assert len(await control._evidence.resume(source)) == 64


async def test_completed_run_requires_finalized_inference(control_fixture):
    control, source, _, _, _, storage, _, _ = control_fixture
    header, freeze, transcript = failed_evidence()
    with pytest.raises(HostedEvidenceError):
        await control.retain(
            source,
            header.model_copy(update={"completed_run": True}),
            freeze,
            chunks(transcript),
        )
    assert storage.puts == 0


async def test_revoke_finds_grant_after_lost_issue_ack(
    control_fixture, monkeypatch, session_maker
):
    control, source, *_ = control_fixture
    original = control._ledger.issue

    async def lost(**kwargs):
        await original(**kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(control._ledger, "issue", lost)
    with pytest.raises(asyncio.CancelledError):
        await control.inference(source)
    await control.revoke(source)
    await control.close(source)
    async with session_maker() as session:
        row = await session.scalar(
            select(CodingHostedInferenceGrant).where(
                CodingHostedInferenceGrant.evaluation_id == source.evaluation_id
            )
        )
        assert row.revoked_at is not None
    with pytest.raises(HostedEvidenceError):
        await control.inference(source)


async def test_replay_keeps_wrapping_identity_after_rotation(
    control_fixture, tmp_path, monkeypatch
):
    control, source, _, _, _, storage, _, _ = control_fixture
    header, freeze, transcript = failed_evidence()
    storage.ambiguous = True
    with pytest.raises(HostedEvidenceError):
        await control.retain(source, header, freeze, chunks(transcript))
    original = control._evidence._existing(source.attempt_id)
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public = tmp_path / "rotated-public.pem"
    public.write_bytes(
        rotated.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    public.chmod(0o600)
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(public)

    async def forbidden(**_kwargs):
        raise AssertionError("existing evidence must not be rewrapped")

    monkeypatch.setattr(wrapper, "wrap_data_key", forbidden)
    control._evidence._wrapper = wrapper
    storage.ambiguous = False
    await control.retain(source, header, freeze, chunks(transcript))
    assert control._evidence._existing(source.attempt_id) == original


async def test_source_drift_and_closed_authority_reject_access(control_fixture):
    control, source, *_ = control_fixture
    await control.check(source)
    with pytest.raises(HostedEvidenceError):
        await control.check(
            source.model_copy(update={"harness_instance_id": "replacement"})
        )
    await control.abort(source)
    with pytest.raises(HostedEvidenceError):
        await control.authoring(source)


async def test_private_socket_requires_token_before_body(control_fixture):
    control, _, *_ = control_fixture
    root = control._root.parent / "auth"
    root.mkdir(mode=0o700)
    from ditto.api_server.coding_hosted_control_server import MAGIC

    server = await HostedControlServer(control, bytes(range(32))).start(
        root / "control.sock"
    )
    try:
        reader, writer = await asyncio.open_unix_connection(root / "control.sock")
        writer.write(MAGIC + b"x" * 32)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        assert not control._bound
    finally:
        server.close()
        await server.wait_closed()
