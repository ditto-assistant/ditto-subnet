from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2PublicationReceipt,
    CodingPrivateV2RegistrationAuthority,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputPublicationStatus,
)
from ditto.api_server.coding_hosted_inputs import (
    HostedAuthoringInputAssembler,
    HostedInputError,
)
from ditto.api_server.coding_private_catalog_v2_compile import (
    compile_private_catalog_v2,
)
from ditto.api_server.coding_private_v2_payload import build_private_v2_payload
from ditto.api_server.coding_private_v2_publication import (
    PrivateV2PublicationObject,
    PrivateV2PublicationReceipt,
    private_v2_publication_signing_message,
    private_v2_remote_object_key,
    write_private_v2_publication_receipt,
)
from ditto.api_server.coding_private_v2_retrieval import (
    PrivateV2InputRetriever,
    PrivateV2RetrievalError,
    PrivateV2UnwrapResult,
)
from ditto.api_server.coding_private_v2_transport import prepare_private_v2_transport
from ditto.tests.api_server.test_coding_private_v2_payload import _bound_fixture
from ditto.tests.api_server.test_coding_private_v2_retrieval import _sha
from ditto.tests.db.queries.test_coding_hosted_private import _freeze, _prepared, _store

REPO = Path(__file__).resolve().parents[5]


@pytest.fixture(scope="session")
def hosted_profile(tmp_path_factory):
    path = tmp_path_factory.mktemp("hosted-profile") / "profile.json"
    subprocess.run(
        [
            "go",
            "test",
            "./internal/codinghostedinput",
            "-run",
            "^TestPlatformAuthoringInputIntegration$",
            "-count=1",
        ],
        cwd=REPO / "services/dittobench-api",
        env={**os.environ, "DITTO_HOSTED_INPUT_PROFILE_OUT": str(path)},
        check=True,
        capture_output=True,
        timeout=180,
    )
    return json.loads(path.read_bytes())


async def fixture(
    tmp_path,
    session_maker,
    hosted_profile,
    *,
    expires_soon=False,
    policy_sha256=None,
    grading_profile_factory=None,
    reader_authority_sha256="6" * 64,
    start=True,
    wrapping_key_callback=None,
):
    tmp_path.chmod(0o700)
    release_path, groups = _bound_fixture(tmp_path, native_authoring=True)
    release = json.loads(release_path.read_bytes())
    compile_private_catalog_v2(
        release_authority=release_path, groups_root=groups, output=tmp_path / "catalog"
    )
    payload = build_private_v2_payload(
        catalog_directory=tmp_path / "catalog",
        groups_root=groups,
        output=tmp_path / "payload",
    )
    wrapping = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    if wrapping_key_callback is not None:
        wrapping_key_callback(wrapping)
    public = tmp_path / "wrapping.pem"
    public.write_bytes(
        wrapping.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    manifest = prepare_private_v2_transport(
        payload_directory=tmp_path / "payload",
        wrapping_public_key=public,
        output=tmp_path / "transport",
    )
    signer = ed25519.Ed25519PrivateKey.generate()
    signer_sha = hashlib.sha256(
        signer.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).hexdigest()
    trusted = tmp_path / "curator.pem"
    trusted.write_bytes(
        signer.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    signature = signer.sign(
        private_v2_publication_signing_message(
            manifest=manifest,
            source_sha="a" * 40,
            probe_receipt_payload_sha256="5" * 64,
            private_input_authority_sha256=reader_authority_sha256,
            curator_signing_key_sha256=signer_sha,
        )
    )
    remote = {}
    rows = []
    for i, item in enumerate(manifest["objects"]):
        key = private_v2_remote_object_key(
            transport_sha256=manifest["transport_sha256"], object_index=i
        )
        remote[key] = (
            tmp_path / "transport" / item["ciphertext_relative_path"]
        ).read_bytes()
        rows.append(
            PrivateV2PublicationObject(
                i,
                hashlib.sha256(key.encode()).hexdigest(),
                item["ciphertext_sha256"],
                item["ciphertext_size_bytes"],
                HippiusPrivateInputPublicationStatus.UPLOADED,
            )
        )
    receipt = PrivateV2PublicationReceipt(
        schema="dittobench-coding-private-v2-publication-v1",
        source_sha="a" * 40,
        checked_at="2026-09-06T00:00:00Z",
        provider="hippius",
        probe_receipt_payload_sha256="5" * 64,
        private_input_authority_sha256=reader_authority_sha256,
        transport_sha256=manifest["transport_sha256"],
        payload_sha256=manifest["payload_sha256"],
        catalog_sha256=manifest["catalog_sha256"],
        catalog_merkle_root=manifest["catalog_merkle_root"],
        wrapping_key_sha256=manifest["wrapping_key_sha256"],
        curator_signing_key_sha256=signer_sha,
        curator_signature_b64=base64.b64encode(signature).decode(),
        object_count=len(rows),
        objects=tuple(rows),
        ready=True,
        shadow_only=True,
        weight_eligible=False,
    )
    receipt_sha = write_private_v2_publication_receipt(
        receipt=receipt, output=tmp_path / "receipt.json"
    )
    registration = {
        "schema": "dittobench-coding-private-v2-registration-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "shadow_only": True,
        "corpus_release_id": release["corpus_release_id"],
        "private_release_sha256": release["release_sha256"],
        "publication_receipt_sha256": receipt_sha,
        "previous_registration_sha256": None,
    }
    registration.update(
        {
            name: manifest[name]
            for name in (
                "catalog_sha256",
                "catalog_merkle_root",
                "payload_sha256",
                "transport_sha256",
                "wrapping_key_sha256",
            )
        }
    )
    registration["registration_sha256"] = _sha(registration)
    registration = CodingPrivateV2RegistrationAuthority.model_validate(registration)
    wire_receipt = CodingPrivateV2PublicationReceipt.model_validate_json(
        (tmp_path / "receipt.json").read_bytes()
    )
    authority, _, worker, grants = await _prepared(
        session_maker,
        start=start,
        registration_bundle=(registration, wire_receipt),
        execution_profile_sha256=hosted_profile["sha256"],
        policy_sha256=policy_sha256,
        grading_profile_sha256=(
            grading_profile_factory(payload["task_assets"][7]["artifacts"])
            if grading_profile_factory
            else None
        ),
        deadline=int(time.time()) + 2 if expires_soon else None,
    )

    class Reader:
        calls = 0
        corrupt = False
        hook = None

        async def get_object(self, *, key, max_bytes):
            self.calls += 1
            value = remote[key]
            assert len(value) == max_bytes
            if self.hook is not None:
                await self.hook()
            return value[:-1] + bytes([value[-1] ^ 1]) if self.corrupt else value

    class Unwrapper:
        calls = 0
        roles = []

        async def unwrap(self, request):
            self.calls += 1
            self.roles.append(request.role)
            key = wrapping.decrypt(
                base64.b64decode(request.wrapped_data_key_b64),
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=bytes.fromhex(request.aad_sha256),
                ),
            )
            return PrivateV2UnwrapResult(request.digest(), key)

    reader, unwrapper = Reader(), Unwrapper()
    retriever = PrivateV2InputRetriever(
        registration=registration,
        transport_manifest=tmp_path / "transport/manifest.json",
        payload_authority=tmp_path / "payload/payload-authority.json",
        publication_receipt=tmp_path / "receipt.json",
        trusted_curator_public_key_path=trusted,
        reader_authority_sha256=reader_authority_sha256,
        audience="platform-authoring",
        grants=_store(session_maker, worker),
        reader=reader,
        unwrapper=unwrapper,
    )
    assembler = HostedAuthoringInputAssembler(
        sessions=session_maker, worker_id=worker, retriever=retriever
    )
    return authority, worker, grants, retriever, assembler, reader, unwrapper


async def test_encrypted_assembly_constructs_native_go_workspace(
    tmp_path, session_maker, hosted_profile
):
    authority, worker, grants, retriever, assembler, reader, unwrapper = await fixture(
        tmp_path, session_maker, hosted_profile
    )
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=grants.authoring_grant_id, role="grader_bundle")
    assert reader.calls == 0
    bundle = await assembler.assemble(
        evaluation_id=authority.evaluation_id,
        attempt_id=authority.attempt_id,
        assignment_sha256=authority.digest(),
    )
    assert (
        reader.calls == unwrapper.calls == 6 and "grader_bundle" not in unwrapper.roles
    )
    assert "print(" not in repr(bundle)
    output = io.BytesIO()
    await bundle.write_private_frame(output)
    frame = tmp_path / "frame.bin"
    frame.write_bytes(output.getvalue())
    frame.chmod(0o600)
    description = await retriever.describe_authoring(grants.authoring_grant_id)
    job = bundle.job
    expected = {
        "evaluation_id": str(job.evaluation_id),
        "attempt_id": str(job.attempt_id),
        "worker_id": str(worker),
        "assignment_sha256": authority.digest(),
        "registration_sha256": authority.registration_sha256,
        "execution_profile_sha256": hosted_profile["sha256"],
        "task_commitment_sha256": description.task_commitment_sha256,
        "deadline_unix": authority.deadline_unix,
        "max_patch_bytes": job.max_patch_bytes,
        "catalog_index": job.catalog_index,
        "corpus_release_id": description.corpus_release_id,
        "private_release_sha256": description.private_release_sha256,
    }
    metadata = tmp_path / "expected.json"
    metadata.write_text(json.dumps(expected))
    metadata.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        "go",
        "test",
        "./internal/codinghostedinput",
        "-run",
        "^TestPlatformAuthoringInputIntegration$",
        "-count=1",
        cwd=REPO / "services/dittobench-api",
        env={
            **os.environ,
            "DITTO_HOSTED_INPUT_FRAME": str(frame),
            "DITTO_HOSTED_INPUT_EXPECTED": str(metadata),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    await _freeze(session_maker, authority, worker)
    with pytest.raises(HostedInputError):
        await bundle.write_private_frame(io.BytesIO())
    with pytest.raises(HostedInputError):
        await assembler.assemble(
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            assignment_sha256=authority.digest(),
        )
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=grants.authoring_grant_id, role="grader_bundle")


@pytest.mark.parametrize(
    "fault", ["assignment", "attempt", "ciphertext", "freeze", "expiry"]
)
async def test_authoring_assembly_fails_closed(
    tmp_path, session_maker, hosted_profile, fault
):
    authority, worker, _, _, assembler, reader, unwrapper = await fixture(
        tmp_path, session_maker, hosted_profile, expires_soon=fault == "expiry"
    )
    args = {
        "evaluation_id": authority.evaluation_id,
        "attempt_id": authority.attempt_id,
        "assignment_sha256": authority.digest(),
    }
    if fault == "assignment":
        args["assignment_sha256"] = "f" * 64
    if fault == "attempt":
        args["attempt_id"] = uuid4()
    if fault == "ciphertext":
        reader.corrupt = True
    if fault == "freeze":
        reader.hook = lambda: _freeze(session_maker, authority, worker)
    if fault == "expiry":
        await asyncio.sleep(2.1)
    with pytest.raises(HostedInputError):
        await assembler.assemble(**args)
    if fault in {"assignment", "attempt", "expiry"}:
        assert reader.calls == 0
    assert unwrapper.calls == 0
