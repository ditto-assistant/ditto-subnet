from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import asdict, replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2PublicationReceipt,
)
from ditto.api_server.coding_hosted_authoring_evidence import canonical
from ditto.api_server.coding_private_v2_custody import (
    PrivateV2Custody,
    PrivateV2CustodyError,
    ProtectedRSAKeyBackend,
)
from ditto.api_server.coding_private_v2_custody_socket import (
    PrivateV2CustodyServer,
    proxy_request,
)
from ditto.api_server.coding_private_v2_retrieval import (
    PrivateV2InputAuthority,
    PrivateV2RetrievalError,
)
from ditto.tests.api_server.test_coding_private_v2_retrieval import (
    KEY,
    NOW,
    PLAIN,
    _fixture,
)
from ditto.tests.db.queries.test_coding_hosted_private import (
    _close,
    _freeze,
    _prepared,
    _store,
)


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


@pytest.fixture
def custody(tmp_path, rsa_key):
    retriever, grants, reader, _ = _fixture(tmp_path, wrapping_key=rsa_key)
    key_file = tmp_path / "custody.pem"
    key_file.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    authority = PrivateV2InputAuthority(
        registration=retriever._registration,
        transport_manifest=tmp_path / "transport.json",
        payload_authority=tmp_path / "payload.json",
        publication_receipt=tmp_path / "receipt.json",
        trusted_curator_public_key_path=tmp_path / "trusted-curator-public.pem",
        reader_authority_sha256="6" * 64,
        audience="platform-authoring",
        clock=lambda: NOW,
    )
    backend = ProtectedRSAKeyBackend(key_file)
    service = PrivateV2Custody(
        authority=authority, grants=grants, backend=backend, clock=lambda: NOW
    )
    return service, retriever, grants, reader, key_file


async def test_real_rsa_gate_integrates_with_encrypted_retriever(custody):
    service, retriever, grants, reader, _ = custody
    request = service._authority.unwrap_request(grants.grant, "issue")
    result = await service.unwrap(request)
    assert result.data_key == KEY and result.request_sha256 == request.digest()
    body = await service.handle(
        canonical({**asdict(request), "future_hint": "ignored"})
    )
    assert set(json.loads(body)) == {
        "schema",
        "request_sha256",
        "data_key_b64",
        "weight_eligible",
    }
    assert base64.b64decode(json.loads(body)["data_key_b64"]) == KEY
    retriever._unwrapper = service
    assert await retriever.read(grant_id=grants.grant.grant_id, role="issue") == PLAIN
    assert reader.calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "legacy-v1"),
        ("evaluation_id", "00000000-0000-0000-0000-000000000099"),
        ("attempt_id", "00000000-0000-0000-0000-000000000098"),
        ("registration_sha256", "a" * 64),
        ("transport_sha256", "a" * 64),
        ("plaintext_sha256", "a" * 64),
        ("ciphertext_sha256", "a" * 64),
        ("wrapping_key_sha256", "a" * 64),
        ("aad_sha256", "a" * 64),
        ("wrapped_data_key_b64", "AAAA"),
        ("audience", "platform-grading"),
        ("phase", "grading"),
        ("role", "grader_bundle"),
        ("frozen_patch_sha256", "b" * 64),
        ("expires_at_unix", NOW + 61),
        ("expires_at_unix", True),
    ],
)
async def test_request_drift_denied_before_key_access(
    custody, monkeypatch, field, value
):
    service, _, grants, _, _ = custody
    request = replace(
        service._authority.unwrap_request(grants.grant, "issue"), **{field: value}
    )
    monkeypatch.setattr(
        "ditto.api_server.coding_private_v2_custody.read_private",
        lambda *_a: pytest.fail("private key accessed"),
    )
    with pytest.raises(
        PrivateV2CustodyError, match="^native v2 key custody unavailable$"
    ):
        await service.unwrap(request)


async def test_revocation_during_backend_never_releases_key(custody):
    service, _, grants, _, _ = custody
    request = service._authority.unwrap_request(grants.grant, "issue")
    original = service._backend.unwrap

    async def revoke(req):
        result = await original(req)
        grants.grant = None
        return result

    service._backend.unwrap = revoke
    with pytest.raises(PrivateV2CustodyError):
        await service.unwrap(request)


async def test_expiry_during_backend_never_releases_key(custody):
    service, _, grants, _, _ = custody
    request = service._authority.unwrap_request(grants.grant, "issue")
    original = service._backend.unwrap

    async def expire(req):
        result = await original(req)
        service._clock = lambda: NOW + 60
        return result

    service._backend.unwrap = expire
    with pytest.raises(PrivateV2CustodyError):
        await service.unwrap(request)


async def test_backend_timeout_is_bounded(custody):
    service, _, grants, _, _ = custody
    grants.grant = replace(grants.grant, expires_at_unix=NOW + 1)
    request = service._authority.unwrap_request(grants.grant, "issue")

    async def hang(_request):
        await asyncio.Event().wait()

    service._backend.unwrap = hang
    with pytest.raises(PrivateV2CustodyError):
        await asyncio.wait_for(service.unwrap(request), 3)


async def test_existing_process_adapter_verifies_native_custody_response(
    custody, tmp_path, monkeypatch
):
    from ditto.api_server.coding_private_v2_unwrap import ProcessPrivateV2Unwrapper

    service, _, grants, _, _ = custody
    request = service._authority.unwrap_request(grants.grant, "issue")
    helper = tmp_path / "proxy"
    helper.write_text("synthetic helper; execution is replaced by private transport")
    helper.chmod(0o500)

    async def run(argv, **kwargs):
        assert argv == (str(helper),)
        return await service.handle(kwargs["body"])

    monkeypatch.setattr(
        "ditto.api_server.coding_private_v2_unwrap.run_private_process", run
    )
    monkeypatch.setattr(
        "ditto.api_server.coding_private_v2_unwrap.time.time", lambda: NOW
    )
    result = await ProcessPrivateV2Unwrapper(
        executable=helper, work_root=tmp_path
    ).unwrap(request)
    assert result.data_key == KEY and result.request_sha256 == request.digest()


async def test_unsafe_private_key_and_merkle_metadata_fail_closed(custody):
    service, retriever, grants, reader, path = custody
    request = service._authority.unwrap_request(grants.grant, "issue")
    path.chmod(0o644)
    with pytest.raises(PrivateV2CustodyError):
        await service.unwrap(request)
    retriever._payload["task_assets"][0]["task_commitment_sha256"] = "0" * 64
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=grants.grant.grant_id, role="issue")
    assert reader.calls == 0


async def test_real_database_phase_and_revocation_control_custody(
    custody, tmp_path, session_maker
):
    import time

    service, retriever, _, _, path = custody
    receipt = CodingPrivateV2PublicationReceipt.model_validate_json(
        (tmp_path / "receipt.json").read_bytes()
    )
    assignment, _, worker, grants = await _prepared(
        session_maker, registration_bundle=(retriever._registration, receipt)
    )

    def service_for(audience):
        authority = PrivateV2InputAuthority(
            registration=retriever._registration,
            transport_manifest=tmp_path / "transport.json",
            payload_authority=tmp_path / "payload.json",
            publication_receipt=tmp_path / "receipt.json",
            trusted_curator_public_key_path=tmp_path / "trusted-curator-public.pem",
            reader_authority_sha256="6" * 64,
            audience=audience,
        )
        store = _store(session_maker, worker, audience)
        return PrivateV2Custody(
            authority=authority, grants=store, backend=ProtectedRSAKeyBackend(path)
        ), store

    authoring, a_store = service_for("platform-authoring")
    grant = await a_store.active_grant(
        grant_id=grants.authoring_grant_id, audience="platform-authoring"
    )
    request = authoring._authority.unwrap_request(grant, "issue")
    assert (await authoring.unwrap(request)).data_key == KEY
    await _freeze(session_maker, assignment, worker)
    with pytest.raises(PrivateV2CustodyError):
        await authoring.unwrap(request)
    grading, g_store = service_for("platform-grading")
    grant = await g_store.active_grant(
        grant_id=grants.grading_grant_id, audience="platform-grading"
    )
    assert grant.expires_at_unix > time.time()
    request = grading._authority.unwrap_request(grant, "grader_bundle")
    assert (await grading.unwrap(request)).data_key == KEY
    await _close(session_maker, assignment, worker)
    with pytest.raises(PrivateV2CustodyError):
        await grading.unwrap(request)


async def test_socket_rejects_real_unauthorized_peer_before_parsing(custody, tmp_path):
    service, _, _, _, _ = custody
    root = tmp_path / "ipc"
    root.mkdir(mode=0o755)
    server = PrivateV2CustodyServer(
        path=root / "custody.sock",
        client_uid=os.geteuid() + 1,
        services={"platform-authoring": service, "platform-grading": service},
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(root / "custody.sock"))
        try:
            assert await asyncio.wait_for(reader.read(), 2) == b""
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await server.close()
    assert not (root / "custody.sock").exists()
    assert server._inode is None
    await server.close()


async def test_real_unix_transport_with_simulated_distinct_credentials(
    custody, tmp_path, monkeypatch
):
    # This tests framing/crypto integration, not actual cross-UID OS isolation.
    service, _, grants, _, _ = custody
    custodian_uid, client_uid = os.geteuid(), os.geteuid() + 1
    root = tmp_path / "ipc"
    root.mkdir(mode=0o755)
    path = root / "custody.sock"
    server = PrivateV2CustodyServer(
        path=path,
        client_uid=client_uid,
        services={"platform-authoring": service, "platform-grading": service},
    )
    await server.start()
    monkeypatch.setattr(
        "ditto.api_server.coding_private_v2_custody_socket.peer_uid",
        lambda writer: (
            custodian_uid
            if writer.get_extra_info("socket").getpeername()
            else client_uid
        ),
    )
    monkeypatch.setattr(
        "ditto.api_server.coding_private_v2_custody_socket.os.geteuid",
        lambda: client_uid,
    )
    request = service._authority.unwrap_request(grants.grant, "issue")
    # Key-file ownership checks must still use the real file owner in this simulation.
    original = service._backend.unwrap

    async def backend(req):
        with monkeypatch.context() as patch:
            patch.setattr(os, "geteuid", lambda: custodian_uid)
            return await original(req)

    service._backend.unwrap = backend
    try:
        result = await proxy_request(
            path=path,
            expected_server_uid=custodian_uid,
            body=canonical(asdict(request)),
        )
        assert base64.b64decode(json.loads(result)["data_key_b64"]) == KEY
    finally:
        await server.close()


def test_server_refuses_shared_uid(custody, tmp_path):
    service = custody[0]
    with pytest.raises(PrivateV2CustodyError):
        PrivateV2CustodyServer(
            path=tmp_path / "socket",
            client_uid=os.geteuid(),
            services={"platform-authoring": service, "platform-grading": service},
        )


async def test_server_never_unlinks_an_existing_path(custody, tmp_path):
    root = tmp_path / "ipc"
    root.mkdir(mode=0o755)
    path = root / "custody.sock"
    path.write_text("retained marker")
    server = PrivateV2CustodyServer(
        path=path,
        client_uid=os.geteuid() + 1,
        services={"platform-authoring": custody[0], "platform-grading": custody[0]},
    )
    with pytest.raises(PrivateV2CustodyError):
        await server.start()
    await server.close()
    assert path.read_text() == "retained marker"
