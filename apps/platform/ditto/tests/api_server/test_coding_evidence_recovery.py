from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_hosted_control import AuthoringIdentity, RetentionHeader
from ditto.api_models.coding_hosted_evidence import HostedEvidenceIdentity
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_server.coding_evidence_recovery import (
    HostedEvidenceRecovery,
    RecoveryTarget,
)
from ditto.api_server.coding_hippius_probe import (
    load_hippius_probe_receipt,
    write_hippius_probe_receipt,
)
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.db.models import (
    CodingHostedEvidenceFinalization,
    CodingHostedEvidenceReservation,
    CodingHostedGradingClaim,
    CodingHostedPrivateTask,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.tests.api_server.test_coding_hosted_control import chunks
from ditto.tests.api_server.test_coding_hosted_control import (
    control_fixture as control_fixture,
)
from ditto.tests.api_server.test_coding_hosted_evidence import evidence as evidence
from ditto.tests.api_server.test_coding_hosted_grading import (
    grader_contract as grader_contract,
)
from ditto.tests.api_server.test_coding_hosted_grading import (
    grading_fixture as grading_fixture,
)
from ditto.tests.api_server.test_coding_hosted_inputs import (
    hosted_profile as hosted_profile,
)


async def prepare_inference(evidence):
    publisher, result, spool, transport, _, args, _, _, root = evidence
    transport.ambiguous = True
    with pytest.raises(HostedEvidenceError):
        await publisher(result)
    raw, body = spool.load(result.settlement.request_id)
    identity = HostedEvidenceIdentity.model_validate_json(raw)
    spool.close()
    transport.ambiguous = False
    target = RecoveryTarget(
        "inference",
        identity.worker_id,
        identity.evaluation_id,
        identity.attempt_id,
        identity.digest(),
        identity.request_id,
    )
    return target, identity, body, root, transport, args


def reader(root):
    return HostedEvidenceSpool(
        root, max_bytes=1 << 30, max_objects=4096, read_only=True
    )


async def test_inference_recovery_uses_reserved_ciphertext_after_lost_ack(
    evidence, session_maker, monkeypatch
):
    target, identity, body, root, transport, args = await prepare_inference(evidence)
    # An unrelated partial capture must be preserved, not block this exact target.
    partial = root / uuid4().hex
    partial.mkdir(mode=0o700)
    (partial / "sealed.bin").write_bytes(b"incomplete synthetic capture")
    spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=args["config"],
            probe_receipt=args["probe_receipt_path"],
            _test_transport=transport,
        )
        assert (await recovery.inspect(target))["state"] == "prepared"
        puts = transport.puts
        report = await recovery.resume(target)
        assert report["state"] == "published" and report["reexecuted"] is False
        assert report["ciphertext_bytes"] == len(body) and transport.puts == puts
        async with session_maker() as session:
            assert (
                await session.get(
                    CodingHostedEvidenceFinalization, identity.reservation_id
                )
                is not None
            )

        async def no_io(**_kwargs):
            pytest.fail("historical acknowledgement contacted provider")

        monkeypatch.setattr(transport, "get_object", no_io)
        assert (await recovery.resume(target))["state"] == "already_finalized"
        assert (partial / "sealed.bin").read_bytes() == b"incomplete synthetic capture"
        with pytest.raises(HostedEvidenceError):
            spool.store(uuid4(), b"{}", b"forbidden")
    finally:
        spool.close()


async def test_spool_requires_exclusive_existing_lock(evidence, tmp_path):
    _, _, spool, _, _, _, _, _, root = evidence
    with pytest.raises(BlockingIOError):
        reader(root)
    empty = tmp_path / "empty-recovery"
    empty.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        reader(empty)
    assert list(empty.iterdir()) == []
    assert not spool.read_only


async def test_unreserved_request_cannot_create_recovery_reservation(
    evidence, session_maker
):
    _, result, spool, transport, _, args, _, _, root = evidence
    spool.close()
    recovery_spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=recovery_spool,
            config=args["config"],
            probe_receipt=args["probe_receipt_path"],
            _test_transport=transport,
        )
        target = RecoveryTarget(
            "inference",
            args["worker_id"],
            uuid4(),
            uuid4(),
            "0" * 64,
            result.settlement.request_id,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(target)
        assert transport.puts == 0 and transport.objects == {}
        async with session_maker() as session:
            assert (
                await session.get(
                    CodingHostedEvidenceReservation, result.settlement.request_id
                )
                is None
            )
    finally:
        recovery_spool.close()


async def test_missing_capture_cannot_be_rebuilt_from_remote(
    evidence, session_maker, tmp_path, monkeypatch
):
    target, _, _, _, transport, args = await prepare_inference(evidence)
    empty = tmp_path / "missing-capture"
    empty.mkdir(mode=0o700)
    HostedEvidenceSpool(empty, max_bytes=1 << 20, max_objects=10).close()

    async def no_io(**_kwargs):
        pytest.fail("missing local capture contacted storage")

    monkeypatch.setattr(transport, "get_object", no_io)
    spool = reader(empty)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=args["config"],
            probe_receipt=args["probe_receipt_path"],
            _test_transport=transport,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(target)
    finally:
        spool.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_id", uuid4()),
        ("attempt_id", uuid4()),
        ("evaluation_id", uuid4()),
        ("identity_sha256", "0" * 64),
    ],
)
async def test_wrong_recovery_target_is_denied_without_io(
    evidence, session_maker, monkeypatch, field, value
):
    target, _, _, root, transport, args = await prepare_inference(evidence)

    async def no_io(**_kwargs):
        pytest.fail("wrong target contacted storage")

    monkeypatch.setattr(transport, "get_object", no_io)
    spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=args["config"],
            probe_receipt=args["probe_receipt_path"],
            _test_transport=transport,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(replace(target, **{field: value}))
    finally:
        spool.close()


async def test_corrupt_remote_bytes_never_overwritten_or_finalized(
    evidence, session_maker
):
    target, identity, _, root, transport, args = await prepare_inference(evidence)
    transport.corrupt = True
    puts = transport.puts
    spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=args["config"],
            probe_receipt=args["probe_receipt_path"],
            _test_transport=transport,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(target)
        assert transport.puts == puts
        async with session_maker() as session:
            assert (
                await session.get(
                    CodingHostedEvidenceFinalization, identity.reservation_id
                )
                is None
            )
    finally:
        spool.close()


@pytest.mark.parametrize("expire_on_read", [1, 2])
async def test_database_clock_expiry_refuses_publication_or_finalization(
    evidence, session_maker, monkeypatch, expire_on_read
):
    target, identity, _, root, transport, args = await prepare_inference(evidence)
    original_scalar = AsyncSession.scalar
    clock_reads = []

    async def scalar(session, statement, *args, **kwargs):
        if str(statement) == "SELECT clock_timestamp() AS clock_timestamp_1":
            clock_reads.append(True)
            if len(clock_reads) == expire_on_read:
                return datetime.fromtimestamp(identity.publication_deadline_unix, UTC)
        return await original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    if expire_on_read == 1:

        async def no_io(**_kwargs):
            pytest.fail("database-expired capture contacted storage")

        monkeypatch.setattr(transport, "get_object", no_io)
    spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=args["config"],
            probe_receipt=args["probe_receipt_path"],
            _test_transport=transport,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(target)
        assert clock_reads == [True] * expire_on_read
        async with session_maker() as session:
            assert (
                await session.get(
                    CodingHostedEvidenceFinalization, identity.reservation_id
                )
                is None
            )
    finally:
        spool.close()


async def test_changed_storage_domain_refused_even_with_matching_fresh_probe(
    evidence, session_maker, tmp_path, monkeypatch
):
    target, _, _, root, transport, args = await prepare_inference(evidence)
    config = replace(args["config"], bucket="different-synthetic-bucket")
    probe, _ = load_hippius_probe_receipt(args["probe_receipt_path"])
    changed = tmp_path / "changed-domain-probe.json"
    write_hippius_probe_receipt(
        receipt=replace(
            probe, sealed_evidence_authority_sha256=config.authority_sha256
        ),
        output=changed,
    )

    async def no_io(**_kwargs):
        pytest.fail("changed domain contacted storage")

    monkeypatch.setattr(transport, "get_object", no_io)
    spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=config,
            probe_receipt=changed,
            _test_transport=transport,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(target)
    finally:
        spool.close()


async def test_stale_probe_blocks_recovery_before_io(
    evidence, session_maker, tmp_path, monkeypatch
):
    target, _, _, root, transport, args = await prepare_inference(evidence)
    probe, _ = load_hippius_probe_receipt(args["probe_receipt_path"])
    old = tmp_path / "stale.json"
    write_hippius_probe_receipt(
        receipt=replace(
            probe,
            checked_at=(datetime.now(UTC) - timedelta(days=2))
            .isoformat()
            .replace("+00:00", "Z"),
        ),
        output=old,
    )

    async def no_io(**_kwargs):
        pytest.fail("stale probe contacted storage")

    monkeypatch.setattr(transport, "get_object", no_io)
    spool = reader(root)
    try:
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=args["config"],
            probe_receipt=old,
            _test_transport=transport,
        )
        with pytest.raises(HostedEvidenceError):
            await recovery.resume(target)
    finally:
        spool.close()


async def test_authoring_recovery_needs_no_plaintext_or_wrap_key(
    control_fixture, session_maker, monkeypatch
):
    control, source, _, _, _, storage, _, _ = control_fixture
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
    publisher = control._evidence
    identity = AuthoringIdentity.model_validate_json(
        canonical(publisher._existing(source.attempt_id)[0])
    )
    root = publisher._spool._path
    publisher._spool.close()
    storage.ambiguous = False

    async def no_encrypt(**_kwargs):
        pytest.fail("recovery re-encrypted")

    monkeypatch.setattr(publisher._wrapper, "wrap_data_key", no_encrypt)
    spool = reader(root)
    try:
        target = RecoveryTarget(
            "authoring",
            source.worker_id,
            source.evaluation_id,
            source.attempt_id,
            identity.digest(),
        )
        probe = control._root.parent / "recovery-probe.json"
        write_hippius_probe_receipt(receipt=publisher._probe, output=probe)
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=publisher._config,
            probe_receipt=probe,
            _test_transport=storage,
        )
        report = await recovery.resume(target)
        assert report["objects"] == identity.chunk_count + 1
        assert report["state"] == "published"
    finally:
        spool.close()


async def test_terminal_recovery_finalizes_without_grading_again(
    grading_fixture, session_maker, monkeypatch, tmp_path
):
    f = grading_fixture
    original_get = f.storage.get_object

    async def corrupt(*, key, max_bytes):
        body = await original_get(key=key, max_bytes=max_bytes)
        return body[:-1] if key.startswith("coding-hosted-terminal/") else body

    monkeypatch.setattr(f.storage, "get_object", corrupt)
    with pytest.raises(AssertionError):
        await f.run()
    async with session_maker() as session:
        row = await session.get(CodingHostedTerminalReservation, f.source.evaluation_id)
        identity = HostedTerminalIdentity.model_validate_json(canonical(row.identity))
        assert (
            await session.get(CodingHostedTerminalFinalization, f.source.evaluation_id)
            is None
        )
    publisher = f.control._evidence
    root = publisher._spool._path
    publisher._spool.close()
    monkeypatch.setattr(f.storage, "get_object", original_get)
    probe = tmp_path / "terminal-recovery-probe.json"
    write_hippius_probe_receipt(receipt=publisher._probe, output=probe)
    spool = reader(root)
    try:
        target = RecoveryTarget(
            "terminal",
            f.source.worker_id,
            f.source.evaluation_id,
            f.source.attempt_id,
            identity.digest(),
        )
        recovery = HostedEvidenceRecovery(
            sessions=session_maker,
            spool=spool,
            config=publisher._config,
            probe_receipt=probe,
            _test_transport=f.storage,
        )
        puts = f.storage.puts
        assert (await recovery.resume(target))["state"] == "published"
        assert f.storage.puts == puts
        async with session_maker() as session:
            assert (
                await session.get(
                    CodingHostedTerminalFinalization, f.source.evaluation_id
                )
                is not None
            )
            assert (
                await session.get(CodingHostedPrivateTask, f.source.evaluation_id)
            ).closed_at is not None
            assert (
                await session.scalar(
                    select(func.count()).select_from(CodingHostedGradingClaim)
                )
                == 1
            )
    finally:
        spool.close()
