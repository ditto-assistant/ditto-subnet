from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ditto.api_models.coding_hosted import hosted_message_digest
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_control_server import HostedControlServer
from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceError
from ditto.api_server.coding_hosted_grading import (
    HostedGradingControl,
    validate_grading_result,
)
from ditto.api_server.coding_hosted_private_grants import HostedPrivateGrantStore
from ditto.api_server.coding_hosted_verification import (
    HostedResultExpectation,
    verify_hosted_result,
)
from ditto.db.models import (
    CodingHostedGradingClaim,
    CodingHostedResultAcknowledgement,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.tests.api_server.endpoints.test_validator_coding_hosted import (
    PATH,
    PLATFORM,
)
from ditto.tests.api_server.endpoints.test_validator_coding_hosted import (
    hosted_client as hosted_client,
)
from ditto.tests.api_server.test_coding_hosted_control import (
    control_fixture,
    decrypt_blob,
)
from ditto.tests.api_server.test_coding_hosted_inputs import (
    REPO,
)
from ditto.tests.api_server.test_coding_hosted_inputs import (
    hosted_profile as hosted_profile,
)
from ditto.tests.api_server.test_coding_hosted_relay import miner_body
from ditto.tests.db.queries.test_coding_hosted_admission import _request


@pytest.fixture(scope="session")
def grader_contract(tmp_path_factory):
    path = tmp_path_factory.mktemp("grader-contract") / "contract.json"
    subprocess.run(
        [
            "go",
            "test",
            "./internal/codinghostedworker",
            "-run",
            "^TestNativeGraderContractExport$",
            "-count=1",
        ],
        cwd=REPO / "services/dittobench-api",
        env={**os.environ, "DITTO_GRADER_CONTRACT_OUT": str(path)},
        capture_output=True,
        check=True,
        timeout=180,
    )
    return json.loads(path.read_bytes())["contract_sha256"]


@pytest.fixture
async def grading_fixture(tmp_path, session_maker, hosted_profile, grader_contract):
    holder = {}

    def profile_factory(artifacts):
        profile = {
            "schema": "dittobench-coding-hosted-grading-profile-v2",
            "image_digest": "sha256:" + "9" * 64,
            "grader_contract_sha256": grader_contract,
            "grader_bundle_sha256": artifacts["grader_bundle"],
            "test_manifest_sha256": "8" * 64,
            "resource_policy": hosted_profile["profile"]["resource_policy"],
            "build": {
                "Required": False,
                "Command": {
                    "ID": "build",
                    "Argv": ["python", "-c", "pass"],
                    "Timeout": 1000000000,
                },
            },
            "test_groups": [
                {
                    "Group": g,
                    "Command": {
                        "ID": g,
                        "Argv": ["dittobench-test-driver", g],
                        "Timeout": 1000000000,
                    },
                    "ExpectedTotal": 1,
                }
                for g in ("hidden", "visible")
            ],
            "execution_timeout": 20_000_000_000,
        }
        holder["profile"] = canonical(profile, 65536)
        return sha(holder["profile"])

    generator = control_fixture.__wrapped__(
        tmp_path, session_maker, hosted_profile, grading_factory=profile_factory
    )
    (
        control,
        source,
        authority,
        grants,
        retriever,
        storage,
        private,
        spools,
    ) = await anext(generator)
    grading_reader = copy.copy(retriever)
    grading_reader._audience = "platform-grading"
    grading_reader._grants = HostedPrivateGrantStore(
        sessions=session_maker, worker_id=source.worker_id, audience="platform-grading"
    )
    grading = HostedGradingControl(
        sessions=session_maker,
        worker_id=source.worker_id,
        retriever=grading_reader,
        authoring_evidence=control._evidence,
        cipher_store=control._evidence,
        profile=holder["profile"],
    )
    control._grading = grading
    root = control._root.parent / "grading-control"
    root.mkdir(mode=0o700)
    socket = root / "control.sock"
    token = bytes(range(32))
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
    output = tmp_path / "graded-result.json"
    config = tmp_path / "graded-config.json"
    config.write_bytes(
        canonical(
            {
                "socket": str(socket),
                "token": base64.b64encode(token).decode(),
                "control": {
                    "Expected": expected,
                    "Profile": json.loads(control._execution),
                    "GradingProfile": base64.b64encode(holder["profile"]).decode(),
                    "GradingProfileSHA256": sha(holder["profile"]),
                },
                "artifact": source.artifact_sha256,
                "agent": str(authority.agent_id),
                "miner_body": base64.b64encode(miner_body()).decode(),
                "output": str(output),
                "grading": True,
            }
        )
    )
    config.chmod(0o600)

    async def run(fault=""):
        settings = json.loads(config.read_bytes())
        settings["grading_fault"] = fault
        config.write_bytes(canonical(settings))
        process = await asyncio.create_subprocess_exec(
            "go",
            "test",
            "./internal/codinghostedworker",
            "-run",
            "^TestPlatformControlAdapterIntegration$",
            "-count=1",
            cwd=REPO / "services/dittobench-api",
            env={**os.environ, "DITTO_HOSTED_CONTROL_TEST": str(config)},
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
        return json.loads(output.read_bytes())

    try:
        yield SimpleNamespace(
            control=control,
            source=source,
            authority=authority,
            grading=grading,
            storage=storage,
            private=private,
            run=run,
        )
    finally:
        server.close()
        await server.wait_closed()
        await generator.aclose()


async def test_connected_grading_seals_evidence_and_returns_signed_result(
    grading_fixture, hosted_client, session_maker
):
    f = grading_fixture
    output = await f.run()
    async with session_maker() as session:
        claim = await session.get(CodingHostedGradingClaim, f.source.evaluation_id)
        row = await session.get(CodingHostedTerminalReservation, f.source.evaluation_id)
        assert (
            await session.get(CodingHostedTerminalFinalization, f.source.evaluation_id)
            is not None
        )
    identity = HostedTerminalIdentity.model_validate_json(canonical(row.identity))
    assert (
        identity.outcome == "completed"
        and identity.digest() == output["terminal_sha256"]
    )
    remote = (
        f"coding-hosted-terminal/v2/{identity.blob.object_id.hex}/"
        f"{identity.blob.ciphertext_sha256}.bin"
    )
    clear = json.loads(
        decrypt_blob(identity.blob, f.storage.objects[remote], f.private)
    )
    assert clear["platform_outcome"] == "completed"
    assert (
        clear["result"]["grader_result"]["result"]["repair_score_micros"] == 1_000_000
    )
    request = _request(f.authority, operation="status")
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 200, response.text
    expected = HostedResultExpectation(
        f.source.evaluation_id,
        f.source.attempt_id,
        f.authority.validator_hotkey,
        PLATFORM.ss58_address,
        f.authority.artifact_sha256,
        f.authority.digest(),
        f.authority.policy_sha256,
        f.authority.execution_profile_sha256,
        f.authority.grading_profile_sha256,
        hosted_message_digest(request),
    )
    result = verify_hosted_result(
        body=response.content,
        expected=expected,
        trusted_verifiers={PLATFORM.ss58_address: PLATFORM},
        now_unix=int(time.time()),
    )
    assert (
        result.evidence_sha256 == identity.digest()
        and result.outcome == "completed"
        and response.headers["cache-control"] == "no-store"
    )
    assert all(
        marker not in response.text
        for marker in (
            "print(",
            "execution_receipts",
            "test_app.py",
            "repair_score_micros",
            "test_groups",
        )
    )
    ack = _request(
        f.authority,
        operation="acknowledge",
        result_sha256=hosted_message_digest(result),
    )
    acknowledged = await hosted_client.post(
        PATH, json=ack.model_dump(mode="json", by_alias=True)
    )
    assert acknowledged.status_code == 204
    assert (
        await hosted_client.post(PATH, json=ack.model_dump(mode="json", by_alias=True))
    ).status_code == 409
    async with session_maker() as session:
        assert (
            await session.get(
                CodingHostedResultAcknowledgement, hosted_message_digest(result)
            )
            is not None
        )
    body = base64.b64decode(output["terminal_body"])
    assert await f.grading.terminal(f.source, body) == identity.digest()
    changed = json.loads(body)
    changed["grader_result"]["result"]["repair_score_micros"] = 0
    with pytest.raises(HostedEvidenceError):
        await f.grading.terminal(f.source, canonical(changed, 4 << 20))
    for cls in (
        CodingHostedGradingClaim,
        CodingHostedTerminalReservation,
        CodingHostedTerminalFinalization,
    ):
        async with session_maker() as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(cls))
    changed = json.loads(body)
    changed["grader_result"]["result"]["execution_receipts"][0]["command_sha256"] = (
        "f" * 64
    )
    with pytest.raises(HostedEvidenceError):
        validate_grading_result(changed, claim.binding)
    for field, value in (
        ("sequence", True),
        ("returncode", False),
        ("total", True),
        ("completed", 1),
    ):
        changed = json.loads(body)
        changed["grader_result"]["result"]["execution_receipts"][0][field] = value
        with pytest.raises(HostedEvidenceError):
            validate_grading_result(changed, claim.binding)
    changed = json.loads(body)
    changed["grader_result"]["result"]["grader"].pop("grader_integrity_before_sha256")
    changed["grader_result"]["result"]["grader"].pop("grader_integrity_after_sha256")
    with pytest.raises(HostedEvidenceError):
        validate_grading_result(changed, claim.binding)
    changed = json.loads(body)
    changed["grader_result"]["result"]["execution_receipts"][0].pop(
        "executor_instance_id"
    )
    with pytest.raises(HostedEvidenceError):
        validate_grading_result(changed, claim.binding)


async def test_hidden_read_requires_freeze_and_claim(grading_fixture, session_maker):
    f = grading_fixture
    with pytest.raises(HostedEvidenceError):
        await f.grading.check(f.source)
    with pytest.raises(HostedEvidenceError):
        await f.grading.check(f.source.model_copy(update={"worker_id": uuid4()}))
    async with session_maker() as session:
        assert (
            await session.get(CodingHostedGradingClaim, f.source.evaluation_id) is None
        )


async def test_failed_preflight_never_reads_hidden_grader(
    grading_fixture, session_maker
):
    f = grading_fixture
    output = await f.run("preflight")
    async with session_maker() as session:
        row = await session.get(CodingHostedTerminalReservation, f.source.evaluation_id)
    identity = HostedTerminalIdentity.model_validate_json(canonical(row.identity))
    assert (
        identity.outcome == "infrastructure_failure"
        and identity.digest() == output["terminal_sha256"]
    )
    assert "grader_bundle" not in f.grading._retriever._unwrapper.roles


async def test_corrupt_terminal_readback_withholds_result_and_replays(
    grading_fixture, session_maker, hosted_client, monkeypatch
):
    f = grading_fixture
    original = f.storage.get_object

    async def corrupt(*, key, max_bytes):
        body = await original(key=key, max_bytes=max_bytes)
        return body[:-1] if key.startswith("coding-hosted-terminal/") else body

    monkeypatch.setattr(f.storage, "get_object", corrupt)
    with pytest.raises(AssertionError):
        await f.run()
    async with session_maker() as session:
        assert (
            await session.get(CodingHostedTerminalFinalization, f.source.evaluation_id)
            is None
        )
    request = _request(f.authority, operation="status")
    pending = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert pending.status_code == 202 and pending.json()["state"] == "started"
    stored = f.control._evidence._existing(
        uuid5(f.source.attempt_id, "hosted-terminal-record-v2")
    )
    assert stored is not None
    identity = HostedTerminalIdentity.model_validate_json(canonical(stored[0]))
    payload = json.loads(decrypt_blob(identity.blob, stored[1], f.private))
    body = canonical(payload["result"], 4 << 20)
    assert sha(body) == identity.result_sha256
    monkeypatch.setattr(f.storage, "get_object", original)
    puts = f.storage.puts
    assert await f.grading.terminal(f.source, body) == identity.digest()
    assert f.storage.puts == puts
    async with session_maker() as session:
        assert len((await session.scalars(select(CodingHostedGradingClaim))).all()) == 1
    request = _request(f.authority, operation="status")
    assert (
        await hosted_client.post(
            PATH, json=request.model_dump(mode="json", by_alias=True)
        )
    ).status_code == 200
