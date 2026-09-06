from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import update

from ditto.api_models.coding_hosted_start import HostedStartRequest
from ditto.api_server import coding_hosted_runtime as runtime
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_probe import (
    load_hippius_probe_receipt,
    write_hippius_probe_receipt,
)
from ditto.api_server.coding_hippius_retrieval import (
    parse_hippius_private_input_retrieval_config,
)
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_launch import (
    HostedLaunchError,
    inspect_launch,
    prepare_launch,
)
from ditto.api_server.coding_hosted_runtime_config import load_runtime_config
from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    read_private,
    run_private_process,
    write_private,
)
from ditto.api_server.coding_hosted_start import HostedStartStore
from ditto.api_server.coding_private_v2_retrieval import PrivateV2RetrievalError
from ditto.db.models import Agent, CodingHostedAssignment
from ditto.db.queries.coding_private_v2_releases import append_private_v2_release_event
from ditto.tests.api_server.test_coding_hippius_evidence import _config, _probe
from ditto.tests.api_server.test_coding_hosted_budget import budget_profile
from ditto.tests.api_server.test_coding_hosted_control import Storage
from ditto.tests.api_server.test_coding_hosted_grading import (
    grader_contract as grader_contract,
)
from ditto.tests.api_server.test_coding_hosted_inference import native_policy
from ditto.tests.api_server.test_coding_hosted_inputs import REPO, fixture
from ditto.tests.api_server.test_coding_hosted_inputs import (
    hosted_profile as hosted_profile,
)
from ditto.tests.api_server.test_coding_hosted_provider import (
    http_response,
    response_body,
)
from ditto.tests.api_server.test_coding_hosted_relay import miner_body
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request
from ditto_screening_protocol import SCREENING_POLICY_VERSION


@pytest.fixture(scope="session")
def hosted_worker_binary(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-launcher")
    root.chmod(0o700)
    path = root / "hosted-worker"
    subprocess.run(
        ["go", "build", "-o", str(path), "./cmd/dittobench-coding-hosted-worker"],
        cwd=REPO / "services/dittobench-api",
        check=True,
        capture_output=True,
        timeout=180,
    )
    path.chmod(0o700)
    return path


class ImageStorage:
    def __init__(self):
        self.calls = []
        self.hook = None

    async def presigned_get_url(self, *, key, expires_in):
        self.calls.append((key, expires_in))
        if self.hook is not None:
            await self.hook()
        return "https://storage.invalid/image.tar?signature=synthetic-private"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.fixture
async def runtime_fixture(
    tmp_path, session_maker, hosted_profile, grader_contract, hosted_worker_binary
):
    tmp_path.chmod(0o700)
    profile = json.loads(json.dumps(hosted_profile))
    profile["profile"]["budgets"]["wall_time_seconds"] = 300
    profile["sha256"] = sha(canonical(profile["profile"]))
    budget = budget_profile(
        valid_from_unix=int(time.time()) - 1, valid_until_unix=int(time.time()) + 3600
    )
    policy = native_policy(runtime_profile_sha256=budget.digest())
    hippius = {
        "DITTO_CODING_HIPPIUS_" + k: v
        for k, v in {
            "ENDPOINT_URL": "https://s3.hippius.com",
            "PRIVATE_INPUT_BUCKET": "coding-private-inputs",
            "PRIVATE_INPUT_CURATOR_ACCESS_KEY": "hip_curator",
            "PRIVATE_INPUT_READER_ACCESS_KEY": "hip_reader",
            "PRIVATE_INPUT_READER_SECRET_KEY": "synthetic-reader-secret",
            "SEALED_EVIDENCE_BUCKET": "coding-sealed-evidence",
            "EVIDENCE_MEDIATOR_ACCESS_KEY": "hip_evidence_mediator",
            "EVIDENCE_MEDIATOR_SECRET_KEY": "evidence-mediator-secret",
            "REGION": "decentralized",
        }.items()
    }
    reader_config = parse_hippius_private_input_retrieval_config(hippius)
    holder = {}

    def grading_profile(artifacts):
        def command(name):
            return {
                "ID": name,
                "Argv": ["dittobench-test-driver", name],
                "Timeout": 1_000_000_000,
            }

        value = {
            "schema": "dittobench-coding-hosted-grading-profile-v2",
            "image_digest": profile["profile"]["image_digest"],
            "grader_contract_sha256": grader_contract,
            "grader_bundle_sha256": artifacts["grader_bundle"],
            "test_manifest_sha256": "8" * 64,
            "resource_policy": profile["profile"]["resource_policy"],
            "build": {"Required": False, "Command": command("build")},
            "test_groups": [
                {"Group": name, "Command": command(name), "ExpectedTotal": 1}
                for name in ("hidden", "visible")
            ],
            "execution_timeout": 20_000_000_000,
        }
        holder["grading"] = canonical(value, 65536)
        return sha(holder["grading"])

    def wrapping_key(key):
        write_private(
            tmp_path / "custody-private.pem",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )

    authority, worker, grants, retriever, _, reader, _ = await fixture(
        tmp_path,
        session_maker,
        profile,
        policy_sha256=policy.digest(),
        grading_profile_factory=grading_profile,
        reader_authority_sha256=reader_config.authority_sha256,
        start=False,
        wrapping_key_callback=wrapping_key,
    )
    await _admit(session_maker, _request(authority))
    async with session_maker() as session, session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == authority.agent_id)
            .values(
                screened_image_ref=f"ditto-screen/{authority.agent_id}:latest",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
    # Synthetic external custody helper permits only these two fixture grants.
    helper = tmp_path / "custody-helper"
    helper.write_text(f"""#!{sys.executable} -I
import base64, hashlib, json, sys
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
r = json.loads(sys.stdin.buffer.read(16385))
assert r["evaluation_id"] == {str(authority.evaluation_id)!r}
assert r["attempt_id"] == {str(authority.attempt_id)!r}
assert r["registration_sha256"] == {authority.registration_sha256!r}
assert r["grant_id"] == {{
    "authoring": {str(grants.authoring_grant_id)!r},
    "grading": {str(grants.grading_grant_id)!r}
}}[r["phase"]]
assert r["audience"] == "platform-" + r["phase"]
key_path = Path({str(tmp_path / "custody-private.pem")!r})
key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
data = key.decrypt(base64.b64decode(r["wrapped_data_key_b64"]), padding.OAEP(
    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(),
    label=bytes.fromhex(r["aad_sha256"])))
raw = (json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    + "\\n").encode()
print(json.dumps({{"schema": "dittobench-coding-private-v2-unwrap-result-v1",
    "request_sha256": hashlib.sha256(raw).hexdigest(),
    "data_key_b64": base64.b64encode(data).decode(), "weight_eligible": False}}))
""")
    helper.chmod(0o700)
    external = tempfile.TemporaryDirectory(prefix="hosted-platform-")
    root = Path(external.name)
    runtime_root = root / "runtime"
    runtime_root.mkdir(mode=0o700)
    docker_listener = await asyncio.start_unix_server(
        lambda _r, w: w.close(), path=root / "docker.sock"
    )
    (root / "docker.sock").chmod(0o600)
    evidence_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    write_private(
        tmp_path / "evidence.pem",
        evidence_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    probe, _ = load_hippius_probe_receipt(_probe(tmp_path, _config()))
    fresh = tmp_path / "fresh-probe.json"
    write_hippius_probe_receipt(
        receipt=replace(
            probe,
            checked_at=datetime.now(UTC).isoformat(),
            private_input_authority_sha256=reader_config.authority_sha256,
        ),
        output=fresh,
    )
    for name, body in {
        "execution.json": canonical(profile["profile"]),
        "grading.json": holder["grading"],
        "budget.json": budget.canonical_bytes(),
        "policy.json": canonical(policy.model_dump(mode="json", by_alias=True)),
        "hippius.json": canonical(hippius),
        "provider-key": b"synthetic-provider-key",
        "image-storage.json": canonical(
            {
                "endpoint_url": "https://storage.invalid",
                "bucket": "screened-images",
                "access_key": "synthetic-image-reader",
                "secret_key": "synthetic-image-secret",
                "region": "test",
            }
        ),
        "postgres.json": json.dumps(
            [
                "POSTGRES_HOST=localhost",
                "POSTGRES_PORT=5432",
                "POSTGRES_USER=synthetic",
                "POSTGRES_PASSWORD=synthetic-db-secret",
                "POSTGRES_DB=synthetic",
            ]
        ).encode(),
    }.items():
        write_private(tmp_path / name, body)
    for path in (tmp_path / "curator.pem", tmp_path / "wrapping.pem"):
        path.chmod(0o600)
    wire = {
        "schema": "dittobench-coding-hosted-platform-runtime-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "evaluation_id": str(authority.evaluation_id),
        "attempt_id": str(authority.attempt_id),
        "worker_id": str(worker),
        "assignment_sha256": authority.digest(),
        "runtime_root": str(runtime_root),
        "worker_executable": str(hosted_worker_binary),
        # This interpreter is only inspected by Go's validation-only command.
        # The real installed Platform start-helper invocation has separate tests.
        "python_executable": shutil.which("python3", path="/usr/bin:/bin"),
        "unwrap_executable": str(helper),
        "unwrap_work_root": str(tmp_path),
        "evidence_wrapping_key_sha256": RsaOaepHippiusEvidenceKeyWrapper(
            tmp_path / "evidence.pem"
        ).wrapping_key_sha256,
        "host": {
            "docker_executable": shutil.which("docker"),
            "docker_socket": str(root / "docker.sock"),
            "router_listen": "172.21.0.1:19010",
            "egress_network": "coding-restricted",
            "egress_proxy": "http://172.21.0.2:3128",
            "executor_repository": "example.invalid/native",
            "candidate_uid": 10001,
            "candidate_gid": 10001,
        },
        **{
            key: str(tmp_path / value)
            for key, value in {
                "postgres_environment_file": "postgres.json",
                "hippius_environment_file": "hippius.json",
                "provider_key_file": "provider-key",
                "image_storage_file": "image-storage.json",
                "execution_profile_file": "execution.json",
                "grading_profile_file": "grading.json",
                "budget_profile_file": "budget.json",
                "policy_file": "policy.json",
                "transport_manifest_file": "transport/manifest.json",
                "payload_authority_file": "payload/payload-authority.json",
                "publication_receipt_file": "receipt.json",
                "curator_public_key_file": "curator.pem",
                "evidence_public_key_file": "evidence.pem",
                "probe_receipt_file": "fresh-probe.json",
            }.items()
        },
    }
    path = tmp_path / "runtime.json"
    write_private(path, canonical(wire, 65536))
    try:
        yield SimpleNamespace(
            path=path,
            config=load_runtime_config(path),
            authority=authority,
            worker=worker,
            grants=grants,
            retriever=retriever,
            reader=reader,
            root=runtime_root,
            wire=wire,
            private=evidence_key,
        )
    finally:
        docker_listener.close()
        await docker_listener.wait_closed()
        external.cleanup()


async def projection_for(f, sessions):
    return await runtime._projection(f.config, sessions)


async def test_projection_is_prestart_metadata_without_private_reads(
    runtime_fixture, session_maker
):
    f = runtime_fixture
    projection = await projection_for(f, session_maker)
    storage = ImageStorage()
    expected, harness = await prepare_launch(
        session_maker,
        projection=projection,
        worker_id=f.worker,
        retriever=f.retriever,
        storage=storage,
    )
    assert expected["catalog_index"] == 7
    assert (
        expected["task_commitment_sha256"]
        == f.retriever.describe_selection(7).task_commitment_sha256
    )
    assert harness["ProfileCapabilityID"] == f"hosted-{f.authority.attempt_id}"
    assert storage.calls == [
        (
            f"{f.authority.agent_id}/screened-images/{projection.image_upload_id}.tar",
            300,
        )
    ]
    assert f.reader.calls == 0
    with pytest.raises(PrivateV2RetrievalError):
        await f.retriever.read(grant_id=f.grants.authoring_grant_id, role="issue")
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, f.authority.evaluation_id)
        assert row.started_at is None and row.worker_id is None


async def test_launch_rechecks_retirement_after_image_url(
    runtime_fixture, session_maker
):
    f = runtime_fixture
    projection = await projection_for(f, session_maker)
    storage = ImageStorage()

    async def retire():
        async with session_maker() as session, session.begin():
            await append_private_v2_release_event(
                session,
                corpus_release_id=projection.registration.corpus_release_id,
                expected_registration_sha256=f.authority.registration_sha256,
                action="retired",
                actor="test",
                reason="synthetic retirement",
            )

    storage.hook = retire
    with pytest.raises(ValueError):
        await prepare_launch(
            session_maker,
            projection=projection,
            worker_id=f.worker,
            retriever=f.retriever,
            storage=storage,
        )
    assert len(storage.calls) == 1 and f.reader.calls == 0


async def test_profile_drift_never_consumes_start(runtime_fixture, session_maker):
    f = runtime_fixture
    with pytest.raises(HostedLaunchError):
        await inspect_launch(
            session_maker,
            evaluation_id=f.authority.evaluation_id,
            attempt_id=f.authority.attempt_id,
            assignment_sha256=f.authority.digest(),
            policy_sha256=f.authority.policy_sha256,
            execution_profile_sha256="f" * 64,
            grading_profile_sha256=f.authority.grading_profile_sha256,
        )
    assert not (f.root / "platform-consumed").exists()


async def install_services(f, sessions, monkeypatch):
    class Reader:
        def __init__(self, config, *, object_namespace):
            assert object_namespace == "v2"
            assert config.authority_sha256 == f.config.reader.authority_sha256

        async def get_object(self, **kwargs):
            return await f.reader.get_object(**kwargs)

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(runtime, "AiobotoHippiusPrivateInputReader", Reader)
    return await runtime.build_runtime_services(
        f.config, sessions, await projection_for(f, sessions)
    )


async def test_factory_and_generated_go_config_complete_native_flow(
    runtime_fixture, session_maker, monkeypatch, tmp_path
):
    f = runtime_fixture
    services = await install_services(f, session_maker, monkeypatch)
    try:
        projection = await projection_for(f, session_maker)
        expected, harness = await prepare_launch(
            session_maker,
            projection=projection,
            worker_id=f.worker,
            retriever=services.retriever,
            storage=ImageStorage(),
        )
        path = runtime.write_worker_config(f.config, services, expected, harness)
        serialized = read_private(path, 65536)
        for secret in (
            b"synthetic-provider-key",
            b"synthetic-reader-secret",
            b"evidence-mediator-secret",
            b"custody-private.pem",
            b"synthetic-image-secret",
        ):
            assert secret not in serialized
        for _ in range(2):
            assert (
                await run_private_process(
                    (
                        f.config.wire.worker_executable,
                        "--validate-only",
                        "--config",
                        str(path),
                    ),
                    root=f.root,
                    timeout=30,
                )
                == b"hosted worker configuration valid\n"
            )
        assert not (f.root / "worker/consumed").exists()
        assert not (f.root / "worker/tmp").exists()
        assert len(services.spools) == 3
        assert services.retriever is not services.control._grading._retriever
        assert services.retriever._audience == "platform-authoring"
        assert services.control._grading._retriever._audience == "platform-grading"
        request = HostedStartRequest.model_validate(
            {
                "schema": "dittobench-coding-hosted-start-v2",
                "evaluation_id": f.authority.evaluation_id,
                "attempt_id": f.authority.attempt_id,
                "worker_id": f.worker,
                "agent_id": f.authority.agent_id,
                "assignment_sha256": f.authority.digest(),
                "artifact_sha256": f.authority.artifact_sha256,
                "screened_image_sha256": f.authority.screened_image_sha256,
                "screened_image_id": projection.image_id,
                "screened_image_ref": projection.image_ref,
                "screened_image_size_bytes": projection.image_size,
                "screening_policy_version": projection.screening_policy_version,
                "profile_capability_id": harness["ProfileCapabilityID"],
                "harness_instance_id": "integration-instance",
                "deadline_unix": f.authority.deadline_unix,
            }
        )
        assert await HostedStartStore(session_maker, f.worker).commit_start(request)
        assert not await HostedStartStore(session_maker, f.worker).commit_start(request)
        with pytest.raises(HostedLaunchError):
            await projection_for(f, session_maker)
        storage = Storage(session_maker)
        services.control._inference._transport = storage
        services.control._evidence._transport = storage
        services.control._grading._cipher._transport = storage
        services.control._test_transport = httpx.MockTransport(
            lambda _r: http_response(response_body())
        )
        output = tmp_path / "result.json"
        test_config = tmp_path / "integration.json"
        write_private(
            test_config,
            canonical(
                {
                    "socket": str(services.socket),
                    "token": base64.b64encode(services.token).decode(),
                    "control": {
                        "Expected": expected,
                        "Profile": json.loads(f.config.execution),
                        "GradingProfile": base64.b64encode(f.config.grading).decode(),
                        "GradingProfileSHA256": sha(f.config.grading),
                    },
                    "artifact": f.authority.artifact_sha256,
                    "agent": str(f.authority.agent_id),
                    "miner_body": base64.b64encode(miner_body()).decode(),
                    "output": str(output),
                    "grading": True,
                },
                65536,
            ),
        )
        process = await asyncio.create_subprocess_exec(
            "go",
            "test",
            "./internal/codinghostedworker",
            "-run",
            "^TestPlatformControlAdapterIntegration$",
            "-count=1",
            cwd=REPO / "services/dittobench-api",
            env={**os.environ, "DITTO_HOSTED_CONTROL_TEST": str(test_config)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(180):
            transcript, _ = await process.communicate()
        assert process.returncode == 0, transcript.decode()
        result = json.loads(read_private(output, 65536))
        terminal = canonical(
            {
                "schema": "dittobench-coding-hosted-runtime-result-v2",
                "shadow_only": True,
                "weight_eligible": False,
                "terminal_evidence_sha256": result["terminal_sha256"],
            }
        )
        assert (
            await runtime.verify_runtime_result(terminal, f.config, session_maker)
            == result["terminal_sha256"]
        )
        assert f.reader.calls >= 7
        assert services.control._grading._cipher._spool is services.spools[2]
    finally:
        await services.shutdown()


async def test_shutdown_drains_unauthenticated_control_handlers(
    runtime_fixture, session_maker, monkeypatch
):
    services = await install_services(runtime_fixture, session_maker, monkeypatch)
    reader, writer = await asyncio.open_unix_connection(services.socket)
    try:
        async with asyncio.timeout(5):
            while not services.server._tasks:
                await asyncio.sleep(0.01)
        async with asyncio.timeout(3):
            await services.shutdown()
        assert not services.server._tasks
        assert await reader.read() == b""
    finally:
        writer.close()
        await writer.wait_closed()


async def test_unfinalized_child_receipt_is_not_success(runtime_fixture, session_maker):
    body = canonical(
        {
            "schema": "dittobench-coding-hosted-runtime-result-v2",
            "shadow_only": True,
            "weight_eligible": False,
            "terminal_evidence_sha256": "a" * 64,
        }
    )
    with pytest.raises(HostedRuntimeError, match="not finalized"):
        await runtime.verify_runtime_result(body, runtime_fixture.config, session_maker)


@pytest.mark.parametrize("fault", ["validation", "unfinalized", "cancelled"])
async def test_process_supervision_never_accepts_unverified_execution(
    runtime_fixture, session_maker, engine, monkeypatch, fault
):
    f = runtime_fixture
    closed = []

    async def close(*_args):
        closed.append(True)

    monkeypatch.setattr(runtime, "create_db_engine", lambda _config: engine)
    monkeypatch.setattr(
        runtime,
        "AiobotoHippiusPrivateInputReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            get_object=f.reader.get_object, __aexit__=close
        ),
    )
    monkeypatch.setattr(runtime, "S3StorageClient", lambda _config: ImageStorage())
    calls = []

    async def child(argv, **_kwargs):
        calls.append(argv[1])
        if argv[1] == "--validate-only":
            return (
                b"wrong"
                if fault == "validation"
                else b"hosted worker configuration valid\n"
            )
        if fault == "cancelled":
            raise asyncio.CancelledError
        return canonical(
            {
                "schema": "dittobench-coding-hosted-runtime-result-v2",
                "shadow_only": True,
                "weight_eligible": False,
                "terminal_evidence_sha256": "a" * 64,
            }
        )

    monkeypatch.setattr(runtime, "run_private_process", child)
    with pytest.raises(
        asyncio.CancelledError if fault == "cancelled" else HostedRuntimeError
    ):
        await runtime.run_runtime(f.path)
    assert calls == (
        ["--validate-only"]
        if fault == "validation"
        else ["--validate-only", "--private-shadow-once"]
    )
    assert closed == [True]
    assert (f.root / "platform-consumed").exists()
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, f.authority.evaluation_id)
        assert row.started_at is None
    with pytest.raises(FileExistsError):
        await runtime.run_runtime(f.path)
    assert len(calls) == (1 if fault == "validation" else 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("weight_eligible", True),
        ("shadow_only", 1),
        ("worker_id", "bad"),
        ("assignment_sha256", "bad"),
    ],
)
async def test_runtime_configuration_rejects_invalid_known_fields(
    runtime_fixture, field, value
):
    f = runtime_fixture
    body = {**f.wire, field: value}
    f.path.write_bytes(canonical(body, 65536))
    with pytest.raises(ValueError):
        load_runtime_config(f.path)
    assert not (f.root / "platform-consumed").exists()


async def test_runtime_rejects_stale_probe_without_start(
    runtime_fixture, session_maker
):
    f = runtime_fixture
    probe, _ = load_hippius_probe_receipt(Path(f.wire["probe_receipt_file"]))
    stale = f.path.parent / "stale-probe.json"
    write_hippius_probe_receipt(
        receipt=replace(probe, checked_at="2026-01-01T00:00:00Z"), output=stale
    )
    changed = replace(
        f.config,
        wire=f.config.wire.model_copy(update={"probe_receipt_file": str(stale)}),
    )
    with pytest.raises(HostedRuntimeError, match="stale"):
        await runtime.build_runtime_services(
            changed, session_maker, await projection_for(f, session_maker)
        )
    assert not (f.root / "control.sock").exists()
