from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_hosted import HostedCodingResult, hosted_message_digest
from ditto.api_server.coding_canary_acceptance import (
    CanaryEvidenceTarget,
    CanaryEvidenceVerifier,
)
from ditto.api_server.coding_evidence_recovery import HostedEvidenceRecovery
from ditto.api_server.coding_hippius_evidence import HippiusSealedEvidenceNotFound
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.api_server.coding_hosted_verification import HostedResultExpectation
from ditto.db.models import (
    CodingHostedInferenceRequest,
    CodingHostedPrivateTask,
    CodingHostedResultAcknowledgement,
)
from ditto.tests.api_server.endpoints.test_validator_coding_hosted import PATH, PLATFORM
from ditto.tests.api_server.endpoints.test_validator_coding_hosted import (
    hosted_client as hosted_client,
)
from ditto.tests.api_server.test_coding_hosted_grading import (
    grader_contract as grader_contract,
)
from ditto.tests.api_server.test_coding_hosted_grading import (
    grading_fixture as grading_fixture,
)
from ditto.tests.api_server.test_coding_hosted_inputs import (
    hosted_profile as hosted_profile,
)
from ditto.tests.db.queries.test_coding_hosted_admission import _request


@pytest.fixture
async def completed_canary(grading_fixture, hosted_client, session_maker, tmp_path):
    f = grading_fixture
    output = await f.run()
    request = _request(f.authority, operation="status")
    response = await hosted_client.post(
        PATH, json=request.model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 200, response.text
    result = HostedCodingResult.model_validate_json(response.content)
    ack = _request(
        f.authority,
        operation="acknowledge",
        result_sha256=hosted_message_digest(result),
    )
    acknowledged = await hosted_client.post(
        PATH, json=ack.model_dump(mode="json", by_alias=True)
    )
    assert acknowledged.status_code == 204
    await f.control.shutdown()
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
    target = CanaryEvidenceTarget(
        expected,
        f.source.worker_id,
        hosted_message_digest(result),
        output["terminal_sha256"],
    )
    publishers = {
        "inference": f.control._inference,
        "authoring": f.control._evidence,
        "terminal": f.grading._cipher,
    }
    spools = {}
    for publisher in publishers.values():
        original = publisher._spool
        original.close()
        if original._path not in spools:
            spools[original._path] = HostedEvidenceSpool(
                original._path, max_bytes=2 << 30, max_objects=4096, read_only=True
            )
    readbacks = {
        phase: HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spools[publisher._spool._path],
            config=publisher._config,
            probe_receipt=tmp_path / "fresh.json",
            _test_transport=f.storage,
        )
        for phase, publisher in publishers.items()
    }
    verifier = CanaryEvidenceVerifier(
        sessions=session_maker,
        readbacks=readbacks,
        trusted_verifiers={PLATFORM.ss58_address: PLATFORM},
    )
    try:
        yield SimpleNamespace(
            body=response.content,
            target=target,
            verifier=verifier,
            storage=f.storage,
            readbacks=readbacks,
        )
    finally:
        for spool in spools.values():
            spool.close()


async def test_fresh_signed_canary_checks_all_phases_without_writes(
    completed_canary, session_maker, engine, monkeypatch
):
    f = completed_canary
    writes, reads = [], []

    def sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", sql)
    get = f.storage.get_object

    async def read(**kwargs):
        reads.append(kwargs["key"])
        return await get(**kwargs)

    async def forbidden(**_kwargs):
        pytest.fail("canary audit attempted a write")

    monkeypatch.setattr(f.storage, "get_object", read)
    monkeypatch.setattr(f.storage, "put_object", forbidden)
    async with session_maker() as session:
        before = await session.scalar(
            select(func.count()).select_from(CodingHostedInferenceRequest)
        )
    try:
        report = await f.verifier.verify(f.body, f.target)
        assert (
            report["canary_evidence_verified"] is True and report["reexecuted"] is False
        )
        assert (
            report["rollout_approved"] is False and report["weight_eligible"] is False
        )
        assert report["pending_acceptance"]
        assert {item["phase"] for item in report["readbacks"]} == {
            "inference",
            "authoring",
            "terminal",
        }
        assert all(item["uploaded"] is False for item in report["readbacks"])
        assert report["inference_requests"] >= 1 and len(reads) >= 4
        assert not writes
        async with session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(CodingHostedInferenceRequest)
                )
                == before
            )
        again = await f.verifier.verify(f.body, f.target)
        assert again["result_sha256"] == report["result_sha256"] and not writes
        assert (
            len(reads) >= 8
        )  # historical finalization is not substituted for a fresh read
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", sql)


@pytest.mark.parametrize("fault", ["target", "missing", "corrupt", "no-trusted-signer"])
async def test_canary_refuses_mismatched_or_missing_evidence(
    completed_canary, monkeypatch, fault
):
    f = completed_canary
    target = f.target
    if fault == "target":
        target = replace(target, terminal_sha256="0" * 64)
    elif fault == "no-trusted-signer":
        f.verifier._verifiers = {}
    else:

        async def read(**_kwargs):
            if fault == "missing":
                raise HippiusSealedEvidenceNotFound("synthetic missing ciphertext")
            return b"synthetic corrupt ciphertext"

        monkeypatch.setattr(f.storage, "get_object", read)

    async def forbidden(**_kwargs):
        pytest.fail("verification tried to repair storage")

    monkeypatch.setattr(f.storage, "put_object", forbidden)
    with pytest.raises(
        HostedEvidenceError, match="canary evidence verification failed"
    ):
        await f.verifier.verify(f.body, target)


async def test_canary_snapshot_is_database_read_only(completed_canary, monkeypatch):
    f = completed_canary
    states = []

    async def write(session, *_args, **_kwargs):
        with pytest.raises(DBAPIError) as error:
            await session.execute(
                text("UPDATE coding_hosted_assignments SET reason=reason WHERE false")
            )
        states.append(getattr(error.value.orig, "sqlstate", None))
        raise HostedEvidenceError("synthetic forbidden write")

    monkeypatch.setattr(f.readbacks["terminal"], "_record", write)
    with pytest.raises(HostedEvidenceError):
        await f.verifier.verify(f.body, f.target)
    assert states == ["25006"]


@pytest.mark.parametrize("fault", ["missing-ack", "open-task"])
async def test_incomplete_lifecycle_refuses_before_remote_reads(
    completed_canary, monkeypatch, fault
):
    f = completed_canary
    original = AsyncSession.get

    async def get(session, entity, *args, **kwargs):
        if entity is CodingHostedResultAcknowledgement and fault == "missing-ack":
            return None
        row = await original(session, entity, *args, **kwargs)
        if entity is CodingHostedPrivateTask and fault == "open-task":
            return SimpleNamespace(closed_at=None)
        return row

    async def forbidden(**_kwargs):
        pytest.fail("incomplete canary reached remote storage")

    monkeypatch.setattr(AsyncSession, "get", get)
    monkeypatch.setattr(f.storage, "get_object", forbidden)
    with pytest.raises(HostedEvidenceError):
        await f.verifier.verify(f.body, f.target)


async def test_cli_bad_envelope_does_not_read_credential_files(monkeypatch):
    from pathlib import Path
    from uuid import uuid4

    from ditto import coding_canary_acceptance as cli
    from ditto.api_server import coding_hosted_runtime_io as io
    from ditto.api_server.coding_hosted_verification import (
        HostedCodingVerificationError,
    )

    paths = []
    raw = {
        "schema": "dittobench-coding-canary-evidence-config-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "worker_id": str(uuid4()),
        "result_sha256": "a" * 64,
        "terminal_sha256": "b" * 64,
        "signed_result_file": "/private/result",
        "postgres_environment_file": "/must-not-read/database",
        "hippius_environment_file": "/must-not-read/storage",
        "expected": {
            "evaluation_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            "validator_hotkey": PLATFORM.ss58_address,
            "platform_hotkey": PLATFORM.ss58_address,
            **dict.fromkeys(
                (
                    "artifact_sha256",
                    "assignment_sha256",
                    "policy_sha256",
                    "execution_profile_sha256",
                    "grading_profile_sha256",
                    "request_sha256",
                ),
                "a" * 64,
            ),
        },
    }

    def read_json(path, *_args):
        paths.append(path)
        if path != Path("/private/config"):
            pytest.fail("bad envelope loaded credentials")
        return raw

    monkeypatch.setattr(io, "read_json", read_json)
    monkeypatch.setattr(io, "read_private", lambda _path, _limit: b"{}")
    with pytest.raises(HostedCodingVerificationError):
        await cli.run(Path("/private/config"))
    assert paths == [Path("/private/config")]


def test_canary_cli_requires_explicit_read_mode(monkeypatch):
    from ditto import coding_canary_acceptance as cli

    monkeypatch.setattr("sys.argv", ["canary", "--config", "/unread/config"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_canary_cli_failure_is_redacted(monkeypatch, capsys):
    from ditto import coding_canary_acceptance as cli

    async def fail(_path):
        raise ValueError("synthetic-private-path-and-secret")

    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr(
        "sys.argv", ["canary", "--verify-evidence", "--config", "/private"]
    )
    assert cli.main() == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "native canary evidence verification unavailable\n"
