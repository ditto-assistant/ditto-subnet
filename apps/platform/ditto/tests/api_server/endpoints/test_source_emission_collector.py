"""Signed claims through durable reveal/payout attribution and source embargo."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.api_server.endpoints.public import _public_artifact_release
from ditto.api_server.ledger_pin import ledger_digest
from ditto.api_server.source_emission_collector import SourceEmissionCollector
from ditto.chain.models import (
    ChainMinerEarning,
    ChainMinerEmissionReceipt,
    ChainWeight,
    ChainWeightVector,
)
from ditto.chain.source_emission_verifier import (
    RevealedVectorUpdate,
    SourceEmissionBlock,
    VotingStake,
    vector_digest,
)
from ditto.db.models import (
    Agent,
    AgentKingship,
    AgentStatus,
    LedgerEpochSnapshot,
    SourceEmissionPayoutResolution,
)
from ditto.db.queries.artifact_release import ArtifactScoreQuorum
from ditto.db.queries.artifact_release_settings import ArtifactReleasePolicy
from ditto.db.queries.king_reign import get_king_reveal
from ditto.db.queries.source_emission_collector import advance_source_emission_cursor
from ditto.tests.api_server.endpoints.test_weight_receipts import (
    _digest,
    _post,
    _setup,
    _signed,
)

pytestmark = pytest.mark.asyncio


def _hash(block):
    return "0x" + f"{block:064x}"


def _observed(raw, block, *, reveal=False, payout=False, unknown=False):
    digest = vector_digest(raw["attempt"]["normalized_weights"])
    updates = ()
    if reveal or unknown:
        updates = (
            RevealedVectorUpdate(
                raw["validator_hotkey"],
                digest,
                None if unknown else raw["attempt"]["ciphertext_hash"],
                None if unknown else raw["attempt"]["commit_block"],
                None if unknown else raw["attempt"]["reveal_round"],
            ),
        )
    return SourceEmissionBlock(
        block,
        _hash(block),
        _hash(block - 1),
        payout,
        updates,
        (VotingStake(2, raw["validator_hotkey"], 65535, True, True),),
        ((raw["validator_hotkey"], digest),),
        "runtime",
    )


def _payout(raw, block):
    return ChainMinerEmissionReceipt(
        raw["netuid"],
        block,
        _hash(block),
        1_789_680_000 + block * 12,
        125,
        block - 10,
        "owner",
        (ChainMinerEarning(1, "miner", 123),),
        (
            ChainWeightVector(
                2,
                raw["validator_hotkey"],
                (
                    ChainWeight(0, "owner", 6553),
                    ChainWeight(1, "miner", 58982),
                ),
            ),
        ),
        (),
        (),
    )


async def _second(raw, maker):
    other = copy.deepcopy(raw)
    other["request_id"] = str(uuid4())
    other["task_id"] += 1
    other["provenance"]["champion_agent_id"] = str(uuid4())
    other["provenance"]["champion_artifact_sha256"] = "cd" * 32
    other["provenance"]["ledger_snapshot_id"] = str(uuid4())
    other["provenance"]["epoch_index"] += 1
    other["attempt"]["attempt_id"] = str(uuid4())
    other["attempt"]["commit_block"] += 20
    other["attempt"]["ciphertext_hex"] = b"other cipher".hex()
    import hashlib

    other["attempt"]["ciphertext_hash"] = hashlib.blake2b(
        b"other cipher", digest_size=32
    ).hexdigest()
    async with maker() as session, session.begin():
        original = await session.get(
            LedgerEpochSnapshot, UUID(raw["provenance"]["ledger_snapshot_id"])
        )
        entries = copy.deepcopy(original.entries)
        entries[0]["agent_id"] = other["provenance"]["champion_agent_id"]
        entries[0]["sha256"] = "cd" * 32
        digest = ledger_digest(entries, original.context["served"])
        other["provenance"]["ledger_digest"] = digest
        session.add(
            LedgerEpochSnapshot(
                snapshot_id=UUID(other["provenance"]["ledger_snapshot_id"]),
                netuid=raw["netuid"],
                epoch_index=124,
                last_epoch_block=119,
                pinned_block=120,
                pinned_block_hash=_hash(120),
                pinned_at=original.pinned_at,
                bench_version=13,
                entries=entries,
                context=original.context,
                ledger_digest=digest,
                champion_agent_id=UUID(other["provenance"]["champion_agent_id"]),
            )
        )
    other["request_digest"] = _digest(
        {
            key: other[key]
            for key in ("schema_version", "mechanism_id", "weights", "provenance")
        }
    )
    return other


async def _prepare(app, maker):
    raw = await _setup(app, maker)
    other = await _second(raw, maker)
    async with maker() as session, session.begin():
        for item in (raw, other):
            session.add(
                Agent(
                    agent_id=UUID(item["provenance"]["champion_agent_id"]),
                    miner_hotkey="miner",
                    name="same-hotkey",
                    sha256=item["provenance"]["champion_artifact_sha256"],
                    size_bytes=500000,
                    status=AgentStatus.SCORED,
                )
            )
        await session.flush()
        session.add(
            AgentKingship(
                agent_id=UUID(other["provenance"]["champion_agent_id"]),
                first_crowned_at=datetime(2026, 1, 1, tzinfo=UTC),
                weight_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        await advance_source_emission_cursor(
            session,
            netuid=raw["netuid"],
            block=199,
            block_hash=_hash(199),
            expected_block=None,
            expected_block_hash=None,
            now=datetime.now(UTC),
        )
    return raw, other


def _collector(app, maker):
    return SourceEmissionCollector(
        app_state=app.state, session_maker=maker, confirmation_enabled=True
    )


async def test_a_paid_with_b_current_only_a_unlocks_then_b_earns_own_tempo(
    app, client, session_maker, monkeypatch
):
    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    a, b = await _prepare(app, session_maker)
    assert (await _post(client, _signed(a))).status_code == 200
    assert (await _post(client, _signed(b))).status_code == 200
    from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
        _HEADERS,
        _install,
    )

    _install(app, session_maker)
    initial = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["release_gate"]
    assert initial["pending_receipt_count"] == 2
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(_observed(a, 201, payout=True), _payout(a, 201))
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1
    status = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["release_gate"]
    assert status["pending_receipt_count"] == 1
    assert status["unresolved_payout_count"] == 0
    assert status["last_payout_attributed"] is True
    assert status["collector_cursor_block"] == 201
    aid, bid = (UUID(item["provenance"]["champion_agent_id"]) for item in (a, b))
    async with session_maker() as session:
        reveals = await get_king_reveal(session, agent_ids=[aid, bid])
        assert reveals[aid].emission_confirmed_at == datetime.fromtimestamp(
            _payout(a, 201).block_timestamp, UTC
        )
        assert reveals[bid].emission_confirmed_at is None
        available = reveals[aid].emission_confirmed_at + timedelta(hours=48)
        for agent_id, expected in ((aid, True), (bid, False)):
            release = _public_artifact_release(
                status=AgentStatus.SCORED,
                score_quorum=ArtifactScoreQuorum(
                    agent_id, 13, datetime(2026, 1, 1, tzinfo=UTC)
                ),
                policy=ArtifactReleasePolicy(SourceDisclosure.PUBLIC, 48),
                king_reveal=reveals[agent_id],
                now=available,
            )
            assert release.download_available is expected
    # Equal-vector unknown write invalidates A; B's pending claim is not enough.
    await collector.process_block(_observed(b, 202, unknown=True), None)
    await collector.process_block(_observed(b, 203, payout=True), _payout(b, 203))
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 0
    await collector.process_block(_observed(b, 204, reveal=True), None)
    await collector.process_block(_observed(b, 205, payout=True), _payout(b, 205))
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1
    restarted = _collector(app, session_maker)
    assert await restarted.resolve_pending_payouts(SimpleNamespace()) == 0
    async with session_maker() as session:
        reveals = await get_king_reveal(session, agent_ids=[aid, bid])
        assert reveals[bid].emission_confirmed_at == datetime.fromtimestamp(
            _payout(b, 205).block_timestamp, UTC
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(SourceEmissionPayoutResolution)
            )
            == 2
        )


async def test_claim_arrives_after_reveal_and_payout_then_restart_recovers_exact_a(
    app, client, session_maker, monkeypatch
):
    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    a, b = await _prepare(app, session_maker)
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(_observed(a, 201, payout=True), _payout(a, 201))
    # New B vectors after A's payout cannot rewrite A's saved consumed bindings.
    await collector.process_block(_observed(b, 202, reveal=True), None)
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 0
    assert (await _post(client, _signed(a))).status_code == 200
    restarted = _collector(app, session_maker)
    assert await restarted.resolve_pending_payouts(SimpleNamespace()) == 1
    assert await restarted.resolve_pending_payouts(SimpleNamespace()) == 0
    async with session_maker() as session:
        rows = list(await session.scalars(select(SourceEmissionPayoutResolution)))
        assert len(rows) == 1
        assert rows[0].agent_id == UUID(a["provenance"]["champion_agent_id"])


async def test_actual_normalized_chain_vector_must_match_declared_pin(
    app,
    client,
    session_maker,
    monkeypatch,
):
    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    a, _ = await _prepare(app, session_maker)
    a["attempt"]["normalized_weights"] = [[0, 32767], [1, 32768]]
    assert (await _post(client, _signed(a))).status_code == 200
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    from dataclasses import replace

    payout = replace(
        _payout(a, 201),
        vectors=(
            ChainWeightVector(
                2,
                a["validator_hotkey"],
                (ChainWeight(0, "owner", 32767), ChainWeight(1, "miner", 32768)),
            ),
        ),
    )
    await collector.process_block(_observed(a, 201, payout=True), payout)
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 0


async def test_late_older_verified_payout_moves_clock_earlier_never_later(
    app,
    client,
    session_maker,
    monkeypatch,
):
    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    a, _ = await _prepare(app, session_maker)
    newer = copy.deepcopy(a)
    newer["request_id"] = str(uuid4())
    newer["task_id"] += 1
    newer["attempt"]["attempt_id"] = str(uuid4())
    newer["attempt"]["commit_block"] += 20
    import hashlib

    newer["attempt"]["ciphertext_hex"] = b"later same agent".hex()
    newer["attempt"]["ciphertext_hash"] = hashlib.blake2b(
        b"later same agent", digest_size=32
    ).hexdigest()
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(_observed(a, 201, payout=True), _payout(a, 201))
    await collector.process_block(_observed(newer, 202, reveal=True), None)
    await collector.process_block(
        _observed(newer, 203, payout=True), _payout(newer, 203)
    )
    # A realtime observer may only notice the crown after this payout occurred.
    async with session_maker() as session, session.begin():
        row = await session.get(
            AgentKingship, UUID(a["provenance"]["champion_agent_id"])
        )
        noticed_at = datetime.fromtimestamp(
            _payout(newer, 203).block_timestamp, UTC
        ) + timedelta(hours=1)
        if row is None:
            session.add(
                AgentKingship(
                    agent_id=UUID(a["provenance"]["champion_agent_id"]),
                    first_crowned_at=noticed_at,
                )
            )
        else:
            row.first_crowned_at = noticed_at
    assert (await _post(client, _signed(newer))).status_code == 200
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1
    assert (await _post(client, _signed(a))).status_code == 200
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1
    async with session_maker() as session:
        row = await session.get(
            AgentKingship, UUID(a["provenance"]["champion_agent_id"])
        )
        assert row.emission_block == 201
        assert row.first_crowned_at <= row.emission_confirmed_at
        assert row.emission_confirmed_at == datetime.fromtimestamp(
            _payout(a, 201).block_timestamp, UTC
        )


async def test_invalid_minority_commit_does_not_abort_good_supermajority(
    app,
    client,
    session_maker,
    monkeypatch,
):
    from dataclasses import replace

    from ditto.api_models.weight_receipt import (
        FinalizedWeightReceipt,
        SubmitWeightReceiptRequest,
    )
    from ditto.db.queries.weight_receipts import record_weight_receipt

    a, _ = await _prepare(app, session_maker)
    assert (await _post(client, _signed(a))).status_code == 200
    bad = copy.deepcopy(a)
    bad["validator_hotkey"] = "bad-validator"
    bad["request_id"] = str(uuid4())
    bad["attempt"]["attempt_id"] = str(uuid4())
    # Insert an already-authenticated minority claim: signature verification is
    # exercised by endpoint tests; this test targets chain-proof rejection.
    async with session_maker() as session, session.begin():
        await record_weight_receipt(
            session,
            submission=SubmitWeightReceiptRequest(
                receipt=FinalizedWeightReceipt.model_validate(bad),
                timestamp=1,
                signature="ab" * 64,
            ),
            now=datetime.now(UTC),
        )

    async def verify(_substrate, *, receipt):
        if receipt.validator_hotkey == "bad-validator":
            raise ValueError("commit extrinsic proof is invalid")

    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit", verify
    )
    collector = _collector(app, session_maker)
    observed = _observed(a, 200, reveal=True)
    bad_update = _observed(bad, 200, reveal=True).updates[0]
    await collector.process_block(
        replace(observed, updates=(*observed.updates, bad_update)), None
    )
    payout_block = _observed(a, 201, payout=True)
    await collector.process_block(
        replace(
            payout_block,
            voting_stake=(
                VotingStake(2, a["validator_hotkey"], 50000, True, True),
                VotingStake(3, "bad-validator", 15535, True, True),
            ),
            vector_digests=(
                *payout_block.vector_digests,
                ("bad-validator", observed.updates[0].vector_digest),
            ),
        ),
        _payout(a, 201),
    )
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1


async def test_unverifiable_payout_is_terminal_but_cursor_advances(
    app,
    session_maker,
):
    from ditto.db.models import SourceEmissionCollectorCursor, SourceEmissionPayout

    a, _ = await _prepare(app, session_maker)
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(
        _observed(a, 201, payout=True),
        None,
        payout_blocked_reason="unverifiable_payout: weights committed during payout",
    )
    await collector.process_block(_observed(a, 202), None)
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == 202
        payout = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert payout.terminal is True
        assert "weights committed" in payout.blocked_reason


async def test_missing_commit_archive_data_retries_provider_and_resolves_payout(
    app, client, session_maker, monkeypatch
):
    from ditto.chain.errors import ChainConnectionError

    a, _ = await _prepare(app, session_maker)
    assert (await _post(client, _signed(a))).status_code == 200
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(_observed(a, 201, payout=True), _payout(a, 201))
    opened = []

    class Archive:
        def __init__(self, *, url):
            self.url = url

        async def __aenter__(self):
            opened.append(self.url)
            return self

        async def __aexit__(self, *args):
            return False

        async def get_chain_finalised_head(self):
            return _hash(201)

        async def get_block_header(self, **_kwargs):
            return {"header": {"number": 201}}

    async def verify(substrate, *, receipt):
        assert receipt.provenance.champion_agent_id == UUID(
            a["provenance"]["champion_agent_id"]
        )
        if substrate.url == "pruned":
            raise ChainConnectionError("historical commit proof is unavailable")

    app.state.chain = SimpleNamespace(
        _historical_substrate_urls=lambda: ["pruned", "archive", "unused"],
        _safe_rpc_error=lambda error: str(error),
    )
    monkeypatch.setattr("async_substrate_interface.AsyncSubstrateInterface", Archive)
    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit", verify
    )
    await collector.sweep()
    assert opened == ["pruned", "archive"]
    async with session_maker() as session:
        row = await session.get(
            AgentKingship, UUID(a["provenance"]["champion_agent_id"])
        )
        assert row.emission_block == 201
        assert (
            await session.scalar(
                select(func.count()).select_from(SourceEmissionPayoutResolution)
            )
            == 1
        )


async def test_same_block_reveal_pays_exact_submission_before_new_same_hotkey_claim(
    app, client, session_maker, monkeypatch
):
    from dataclasses import replace

    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    a, b = await _prepare(app, session_maker)
    for raw in (a, b):
        assert (await _post(client, _signed(raw))).status_code == 200
    collector = _collector(app, session_maker)
    aid, bid = (UUID(item["provenance"]["champion_agent_id"]) for item in (a, b))
    for raw, block in ((a, 200), (b, 201)):
        observed = replace(
            _observed(raw, block, reveal=True, payout=True),
            payout_initialization_reveals=True,
        )
        await collector.process_block(observed, _payout(raw, block))
        assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1
        async with session_maker() as session:
            reveals = await get_king_reveal(session, agent_ids=[aid, bid])
            assert reveals[aid].emission_confirmed_at == datetime.fromtimestamp(
                _payout(a, 200).block_timestamp, UTC
            )
            if block == 200:
                assert reveals[bid].emission_confirmed_at is None
            else:
                assert reveals[bid].emission_confirmed_at == datetime.fromtimestamp(
                    _payout(b, 201).block_timestamp, UTC
                )
    assert (
        await _collector(app, session_maker).resolve_pending_payouts(SimpleNamespace())
        == 0
    )


async def test_v467_transition_recovers_on_the_second_archive(
    app, session_maker, monkeypatch
):
    from ditto.chain.errors import ChainEmissionReceiptUnavailable
    from ditto.db.models import SourceEmissionCollectorCursor, SourceEmissionPayout

    previous = "0xff4ba0da10fb8ac26fab3e446f23413ef7f91de4a604802097ece0b928d53a8e"
    runtime = "0x2f175dcc64196ec8a6b9235f8d7cfd84efef6c68bb925c4455949591cef9f6d2"
    cursor_block = 9095775
    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = cursor_block
        cursor.block_hash = _hash(cursor_block)
        cursor.runtime_code_hash = previous
    collector = _collector(app, session_maker)
    opened = []

    def observed(block, *, payout=False, reset=None):
        base = _observed(a, block, payout=payout)
        return SourceEmissionBlock(
            base.block,
            base.block_hash,
            base.parent_hash,
            False if reset else base.is_payout,
            () if reset else base.updates,
            base.voting_stake,
            base.vector_digests,
            runtime,
            reset,
        )

    class Archive:
        def __init__(self, *, url):
            self.url = url

        async def __aenter__(self):
            opened.append(self.url)
            return self

        async def __aexit__(self, *args):
            return False

        async def get_chain_finalised_head(self):
            return _hash(cursor_block + 3)

        async def get_block_header(self, **_kwargs):
            return {"header": {"number": cursor_block + 3}}

    async def read(
        substrate, *, netuid, block, expected_runtime_code_hash=None, **_reads
    ):
        assert netuid == a["netuid"]
        assert expected_runtime_code_hash is None
        if substrate.url == "pruned":
            raise ValueError(f"runtime fingerprint is not audited: {runtime}")
        if block == cursor_block + 1:
            return observed(block, reset="runtime_changed")
        if block == cursor_block + 2:
            return observed(block)
        return observed(block, payout=True)

    async def receipt(*_args, **_kwargs):
        raise ChainEmissionReceiptUnavailable("historical receipt is unavailable")

    app.state.chain = SimpleNamespace(
        _historical_substrate_urls=lambda: ["pruned", "archive"],
        _safe_rpc_error=lambda error: str(error),
        get_miner_emission_receipt=receipt,
    )
    monkeypatch.setattr("async_substrate_interface.AsyncSubstrateInterface", Archive)
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block",
        read,
    )
    await collector.sweep()
    assert opened == ["pruned", "archive"]
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == cursor_block + 3
        assert cursor.block_hash == _hash(cursor_block + 3)
        assert cursor.runtime_code_hash == runtime
        assert cursor.last_blocked_reason is None
        payout = await session.get(
            SourceEmissionPayout, (a["netuid"], _hash(cursor_block + 3))
        )
        assert payout is not None and payout.terminal is True
        assert payout.proof["runtime_code_hash"] == runtime
        assert (
            await session.get(
                SourceEmissionPayout, (a["netuid"], _hash(cursor_block + 1))
            )
            is None
        )


@pytest.mark.parametrize("finalized", [202, 15000])
async def test_pending_payout_resolves_despite_failing_block_scan(
    app, client, session_maker, monkeypatch, finalized
):
    a, b = await _prepare(app, session_maker)
    assert (await _post(client, _signed(a))).status_code == 200
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(_observed(a, 201, payout=True), _payout(a, 201))
    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    scan = AsyncMock(side_effect=RuntimeError("RPC work limit exceeded"))
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block", scan
    )
    substrate = SimpleNamespace(
        get_chain_finalised_head=AsyncMock(return_value=_hash(finalized)),
        get_block_header=AsyncMock(return_value={"header": {"number": finalized}}),
    )
    with pytest.raises(RuntimeError, match="RPC work limit exceeded"):
        await collector._sweep_provider(substrate)
    scan.assert_awaited_once()
    async with session_maker() as session:
        from ditto.db.models import SourceEmissionCollectorCursor

        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == 201
        assert cursor.block_hash == _hash(201)
        aid = UUID(a["provenance"]["champion_agent_id"])
        bid = UUID(b["provenance"]["champion_agent_id"])
        reveals = await get_king_reveal(session, agent_ids=[aid, bid])
        assert reveals[aid].emission_confirmed_at == datetime.fromtimestamp(
            _payout(a, 201).block_timestamp, UTC
        )
        assert bid not in reveals or reveals[bid].emission_confirmed_at is None


async def test_sweep_widens_batch_and_skips_leading_resolution_in_catchup(
    app, session_maker, monkeypatch
):
    """A deep backlog scans a wide batch; the leading payout resolution, which
    re-reads the same historical evidence every sweep, waits for the batch end."""
    from ditto.db.models import SourceEmissionCollectorCursor

    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = 100
        cursor.block_hash = _hash(100)
    collector = _collector(app, session_maker)
    scanned = []

    async def read(
        _substrate, *, netuid, block, expected_runtime_code_hash=None, **_reads
    ):
        del netuid, expected_runtime_code_hash
        scanned.append(block)
        return _observed(a, block)

    resolved = []

    async def resolve(_substrate, **_kwargs):
        resolved.append(True)
        return 0

    substrate = SimpleNamespace(
        get_chain_finalised_head=AsyncMock(return_value=_hash(15000)),
        get_block_header=AsyncMock(return_value={"header": {"number": 15000}}),
    )
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block",
        read,
    )
    monkeypatch.setattr(collector, "resolve_pending_payouts", resolve)
    await collector._sweep_provider(substrate)
    assert scanned == list(range(101, 229))
    assert len(resolved) == 1


async def test_failed_catchup_scan_still_resolves_pending_payouts(
    app, session_maker, monkeypatch
):
    """A mid-batch archive failure during deep catch-up must not starve payout
    resolution: the resolver cycles pending rows with its own archive reads."""
    from ditto.db.models import SourceEmissionCollectorCursor

    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = 100
        cursor.block_hash = _hash(100)
    collector = _collector(app, session_maker)

    async def read(
        _substrate, *, netuid, block, expected_runtime_code_hash=None, **_reads
    ):  # noqa: ARG001
        del netuid, block, expected_runtime_code_hash
        raise RuntimeError("Historical work rate limit exceeded")

    resolved = []

    async def resolve(_substrate, **_kwargs):
        resolved.append(True)
        return 0

    substrate = SimpleNamespace(
        get_chain_finalised_head=AsyncMock(return_value=_hash(15000)),
        get_block_header=AsyncMock(return_value={"header": {"number": 15000}}),
    )
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block",
        read,
    )
    monkeypatch.setattr(collector, "resolve_pending_payouts", resolve)
    with pytest.raises(RuntimeError, match="rate limit"):
        await collector._sweep_provider(substrate)
    assert len(resolved) == 1


async def test_resolver_failure_cannot_mask_why_the_scan_stopped(
    app, session_maker, monkeypatch
):
    """The 2026-10 stall read as a SCALE decoder error from the resolver for
    weeks; the scan's own refusal (an unaudited runtime) is what operators need."""
    from ditto.db.models import SourceEmissionCollectorCursor

    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = 100
        cursor.block_hash = _hash(100)
    collector = _collector(app, session_maker)

    async def read(_substrate, **_reads):
        raise ValueError("runtime fingerprint is not audited: 0xnew")

    async def resolve(_substrate, **_kwargs):
        raise AttributeError("'scale_info::460' object has no attribute 'index'")

    substrate = SimpleNamespace(
        get_chain_finalised_head=AsyncMock(return_value=_hash(15000)),
        get_block_header=AsyncMock(return_value={"header": {"number": 15000}}),
    )
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block",
        read,
    )
    monkeypatch.setattr(collector, "resolve_pending_payouts", resolve)
    with pytest.raises(ValueError, match="not audited: 0xnew"):
        await collector._sweep_provider(substrate)


async def test_sweep_keeps_steady_state_batch_and_leading_resolution(
    app, session_maker, monkeypatch
):
    from ditto.db.models import SourceEmissionCollectorCursor

    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = 200
        cursor.block_hash = _hash(200)
    collector = _collector(app, session_maker)
    scanned = []

    async def read(
        _substrate, *, netuid, block, expected_runtime_code_hash=None, **_reads
    ):
        del netuid, expected_runtime_code_hash
        scanned.append(block)
        return _observed(a, block)

    resolved = []

    async def resolve(_substrate, **_kwargs):
        resolved.append(True)
        return 0

    substrate = SimpleNamespace(
        get_chain_finalised_head=AsyncMock(return_value=_hash(204)),
        get_block_header=AsyncMock(return_value={"header": {"number": 204}}),
    )
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block",
        read,
    )
    monkeypatch.setattr(collector, "resolve_pending_payouts", resolve)
    await collector._sweep_provider(substrate)
    assert scanned == list(range(201, 205))
    assert len(resolved) == 2


async def test_sweep_timeout_tracks_backlog_depth(app, session_maker, monkeypatch):
    from ditto.db.models import SourceEmissionCollectorCursor

    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = 100
        cursor.block_hash = _hash(100)

    async def read(
        _substrate, *, netuid, block, expected_runtime_code_hash=None, **_reads
    ):
        del netuid, expected_runtime_code_hash
        return _observed(a, block)

    collector = _collector(app, session_maker)

    class Archive:
        def __init__(self, *, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get_chain_finalised_head(self):
            return _hash(15000)

        async def get_block_header(self, **_kwargs):
            return {"header": {"number": 15000}}

    timeouts = []
    rescheduled = []

    import ditto.api_server.source_emission_collector as collector_module

    real_timeout = collector_module.asyncio.timeout

    class TimeoutProbe:
        def __init__(self, seconds):
            self.seconds = seconds
            timeouts.append(seconds)
            self._inner = real_timeout(seconds)

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        def when(self):
            return self._inner.when()

        def reschedule(self, when):
            loop = collector_module.asyncio.get_running_loop()
            rescheduled.append(round(when - loop.time()))
            self._inner.reschedule(when)

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    monkeypatch.setattr(collector_module.asyncio, "timeout", TimeoutProbe)
    monkeypatch.setattr(
        "ditto.api_server.source_emission_collector.read_source_emission_block",
        read,
    )
    import async_substrate_interface

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", Archive)
    app.state.chain = SimpleNamespace(
        _historical_substrate_urls=lambda: ["archive"],
        _safe_rpc_error=lambda error: str(error),
    )
    # The first sweep opens with the steady-state budget because it has not
    # measured the backlog yet. Finding a deep one must extend that same sweep
    # to the catch-up budget its 128-block batch needs, and later sweeps start
    # there.
    await collector.sweep()
    assert collector._backlog_deep is True
    assert timeouts == [120.0]
    # Then the finished sweep hands the shadow its own 90 s + 15 s window.
    shadow = round(
        collector_module._REVEAL_PLAN_SHADOW_SECONDS
        + collector_module._REVEAL_PLAN_SHADOW_RESERVE_SECONDS
    )
    assert rescheduled == [600, shadow]
    await collector.sweep()
    assert timeouts == [120.0, 600.0]
    assert rescheduled == [600, shadow, 600, shadow]


_OWNERSHIP = "subnet ownership changed during distribution block"


async def test_receipt_ambiguity_must_reproduce_before_terminal(app, session_maker):
    """One inconsistent archive read must not discard a real payout."""
    from ditto.chain.errors import ChainEmissionReceiptUnavailable

    a, _ = await _prepare(app, session_maker)
    collector = _collector(app, session_maker)

    def chain(*outcomes):
        calls = []

        async def receipt(*_args, **kwargs):
            calls.append(kwargs["payout_block"])
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        app.state.chain = SimpleNamespace(get_miner_emission_receipt=receipt)
        return calls

    calls = chain(ChainEmissionReceiptUnavailable(_OWNERSHIP), _payout(a, 201))
    got = await collector._confirmed_receipt(
        SimpleNamespace(), block=201, allow_initialization_reveals=False
    )
    assert got == _payout(a, 201) and calls == [201, 201]

    chain(
        ChainEmissionReceiptUnavailable(_OWNERSHIP),
        ChainEmissionReceiptUnavailable("weights changed during distribution block"),
    )
    with pytest.raises(ValueError, match="did not reproduce"):
        await collector._confirmed_receipt(
            SimpleNamespace(), block=201, allow_initialization_reveals=False
        )

    chain(
        ChainEmissionReceiptUnavailable(_OWNERSHIP),
        ChainEmissionReceiptUnavailable(_OWNERSHIP),
    )
    with pytest.raises(ChainEmissionReceiptUnavailable, match="ownership"):
        await collector._confirmed_receipt(
            SimpleNamespace(), block=201, allow_initialization_reveals=False
        )


async def _reopened_payout(app, client, session_maker, monkeypatch):
    from ditto.db.models import SourceEmissionPayout

    monkeypatch.setattr(
        "ditto.chain.source_emission_verifier.verify_finalized_weight_commit",
        AsyncMock(),
    )
    a, _ = await _prepare(app, session_maker)
    assert (await _post(client, _signed(a))).status_code == 200
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    await collector.process_block(
        _observed(a, 201, payout=True),
        None,
        payout_blocked_reason=f"unverifiable_payout: {_OWNERSHIP}",
    )
    async with session_maker() as session, session.begin():
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert row.terminal is True and row.proof["receipt"] is None
        # What the reopening migration does for each false verdict.
        row.terminal = False
    return a, collector


async def test_reopened_receiptless_payout_refetches_and_confirms(
    app, client, session_maker, monkeypatch
):
    from ditto.db.models import SourceEmissionPayout

    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)
    receipt = AsyncMock(return_value=_payout(a, 201))
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=receipt, _safe_rpc_error=str
    )
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1
    assert receipt.await_args.kwargs["payout_block"] == 201
    assert receipt.await_args.kwargs["allow_initialization_reveals"] is False
    aid = UUID(a["provenance"]["champion_agent_id"])
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert row.terminal is False
        assert row.proof["receipt"]["block"] == 201
        assert row.proof["receipt"]["earnings"] == [
            {"uid": 1, "hotkey": "miner", "amount_rao": 123}
        ]
        reveals = await get_king_reveal(session, agent_ids=[aid])
        assert reveals[aid].emission_confirmed_at == datetime.fromtimestamp(
            _payout(a, 201).block_timestamp, UTC
        )
    # The persisted receipt is reused; no further historical reads.
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 0
    receipt.assert_awaited_once()


async def test_payout_resolution_stops_at_its_deadline_and_resumes_later(
    app, client, session_maker, monkeypatch
):
    """A spent deadline leaves rows untouched for the next sweep."""
    from ditto.db.models import SourceEmissionPayout

    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)
    receipt = AsyncMock(return_value=_payout(a, 201))
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=receipt, _safe_rpc_error=str
    )
    past = asyncio.get_running_loop().time() - 1
    assert (
        await collector.resolve_pending_payouts(SimpleNamespace(), deadline=past) == 0
    )
    receipt.assert_not_awaited()
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert row.last_checked_at is None
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1


async def test_a_payout_row_cut_short_by_the_deadline_stays_pending(
    app, client, session_maker, monkeypatch
):
    from ditto.db.models import SourceEmissionPayout

    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)

    async def slow_receipt(**_kwargs):
        await asyncio.sleep(5)

    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=slow_receipt, _safe_rpc_error=str
    )
    soon = asyncio.get_running_loop().time() + 0.05
    assert (
        await collector.resolve_pending_payouts(SimpleNamespace(), deadline=soon) == 0
    )
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        # At most a transient retry note: never terminal, never confirmed.
        assert row.terminal is False
        assert row.blocked_reason is None or row.blocked_reason.startswith(
            "receipt_retry"
        )


async def test_a_provider_timeout_is_not_mistaken_for_the_deadline(
    app, client, session_maker, monkeypatch
):
    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)
    del a
    monkeypatch.setattr(
        collector, "_refetch_receipt", AsyncMock(side_effect=TimeoutError)
    )
    later = asyncio.get_running_loop().time() + 60
    with pytest.raises(TimeoutError):
        await collector.resolve_pending_payouts(SimpleNamespace(), deadline=later)


async def test_reopened_payout_reproduced_ambiguity_is_terminal_again(
    app, client, session_maker, monkeypatch
):
    from ditto.chain.errors import ChainEmissionReceiptUnavailable
    from ditto.db.models import SourceEmissionPayout

    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)
    receipt = AsyncMock(side_effect=ChainEmissionReceiptUnavailable(_OWNERSHIP))
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=receipt, _safe_rpc_error=str
    )
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 0
    assert receipt.await_count == 2
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert row.terminal is True
        assert row.blocked_reason == f"unverifiable_payout: {_OWNERSHIP}"
        assert row.proof["receipt"] is None


async def test_reopened_payout_transient_failure_stays_pending(
    app, client, session_maker, monkeypatch
):
    from ditto.db.models import SourceEmissionPayout

    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=AsyncMock(
            side_effect=RuntimeError("Historical work rate limit exceeded")
        ),
        _safe_rpc_error=str,
    )
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 0
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert row.terminal is False
        assert row.last_checked_at is not None
        assert row.blocked_reason.startswith("receipt_retry: Historical work")
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=AsyncMock(return_value=_payout(a, 201)),
        _safe_rpc_error=str,
    )
    assert await collector.resolve_pending_payouts(SimpleNamespace()) == 1


async def test_receiptless_refetches_are_bounded_per_resolution(app, session_maker):
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionPayout

    a, _ = await _prepare(app, session_maker)
    collector = _collector(app, session_maker)
    await collector.process_block(_observed(a, 200, reveal=True), None)
    for block in range(201, 201 + module._RECEIPT_REFETCH_LIMIT + 2):
        await collector.process_block(
            _observed(a, block, payout=True),
            None,
            payout_blocked_reason=f"unverifiable_payout: {_OWNERSHIP}",
        )
    async with session_maker() as session, session.begin():
        for row in await session.scalars(select(SourceEmissionPayout)):
            row.terminal = False
    receipt = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=receipt, _safe_rpc_error=str
    )
    await collector.resolve_pending_payouts(SimpleNamespace())
    assert receipt.await_count == module._RECEIPT_REFETCH_LIMIT


async def test_refetch_does_not_touch_a_payout_resolved_meanwhile(
    app, client, session_maker, monkeypatch
):
    from ditto.db.models import SourceEmissionPayout

    a, collector = await _reopened_payout(app, client, session_maker, monkeypatch)
    aid = UUID(a["provenance"]["champion_agent_id"])

    async def receipt(*_args, **_kwargs):
        # Another collector settles the payout while this read is in flight.
        async with session_maker() as session, session.begin():
            session.add(
                SourceEmissionPayoutResolution(
                    netuid=a["netuid"],
                    block_hash=_hash(201),
                    agent_id=aid,
                    artifact_sha256=a["provenance"]["champion_artifact_sha256"],
                    proof={},
                    created_at=datetime.now(UTC),
                )
            )
        return _payout(a, 201)

    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=receipt, _safe_rpc_error=str
    )
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        snapshot = dict(row.proof)
    got = await collector._refetch_receipt(
        SimpleNamespace(), block=201, block_hash=_hash(201), snapshot=snapshot
    )
    assert got is None
    async with session_maker() as session:
        row = await session.get(SourceEmissionPayout, (a["netuid"], _hash(201)))
        assert row.proof["receipt"] is None and row.last_checked_at is None


@pytest.fixture(autouse=True)
def _event_planner_mode(monkeypatch):
    """The sparse tests below stub the event planner, so they pin shadow mode.

    Production runs "primary"; the primary-mode tests override this.
    """
    from ditto.api_server import source_emission_collector as module

    monkeypatch.setattr(module, "_REVEAL_PLAN_MODE", "shadow")


def _sparse_plan(current, end, interesting, *, full_scan=False):
    from ditto.chain.source_emission_verifier import SourceEmissionRangePlan

    return SourceEmissionRangePlan(
        current + 1,
        end,
        {b: _hash(b) for b in range(current, end + 1)},
        tuple(interesting),
        full_scan,
        "runtime",
    )


async def _deep_backlog(app, session_maker, cursor_block):
    from ditto.db.models import SourceEmissionCollectorCursor

    a, _ = await _prepare(app, session_maker)
    async with session_maker() as session, session.begin():
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        cursor.block = cursor_block
        cursor.block_hash = _hash(cursor_block)
    substrate = SimpleNamespace(
        get_chain_finalised_head=AsyncMock(return_value=_hash(15000)),
        get_block_header=AsyncMock(return_value={"header": {"number": 15000}}),
    )
    return a, substrate


async def test_sparse_catchup_reads_only_planned_blocks_and_skips_the_rest(
    app, session_maker, monkeypatch
):
    """Quiet blocks commit as cursor skips; planned blocks get the full reader."""
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionCollectorCursor, SourceEmissionPayout

    a, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    planned = (150, 151)
    monkeypatch.setattr(
        module,
        "plan_source_emission_range",
        AsyncMock(return_value=_sparse_plan(100, end, planned)),
    )
    read_blocks = []

    async def read(_substrate, *, netuid, block, **_kwargs):
        del netuid
        read_blocks.append(block)
        if block == 150:
            return _observed(a, block, reveal=True)
        if block == 151:
            return _observed(a, block, payout=True)
        return _observed(a, block)

    monkeypatch.setattr(module, "read_source_emission_block", read)
    monkeypatch.setattr(module.random, "sample", lambda seq, k: list(seq)[:k])
    receipt = AsyncMock(return_value=_payout(a, 151))
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=receipt, _safe_rpc_error=str
    )
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))

    await collector._sweep_provider(substrate)

    # Two audit samples (the first quiet blocks) plus the two planned blocks.
    assert read_blocks == [101, 102, 150, 151]
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert (cursor.block, cursor.block_hash) == (end, _hash(end))
        assert cursor.runtime_code_hash == "runtime"
        payout = await session.get(SourceEmissionPayout, (a["netuid"], _hash(151)))
        assert payout is not None and payout.proof["receipt"]["block"] == 151


async def _shadowed_sweep(app, session_maker, monkeypatch, reveal_plan):
    """Replay one sparse range with blocks 150 (reveal) and 151 (payout).

    ``reveal_plan`` stands in for the reveal-anchored planner the shadow runs.
    Returns the netuid row's shadow summary after the sweep.
    """
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionCollectorCursor

    a, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    monkeypatch.setattr(
        module,
        "plan_source_emission_range",
        AsyncMock(return_value=_sparse_plan(100, end, (150, 151, 160))),
    )
    monkeypatch.setattr(module, "plan_source_emission_range_by_reveals", reveal_plan)

    async def read(_substrate, *, netuid, block, **_kwargs):
        del netuid
        if block == 150:
            return _observed(a, block, reveal=True)
        if block == 151:
            return _observed(a, block, payout=True)
        # 160 was planned by the event planner but turned out quiet.
        return _observed(a, block)

    monkeypatch.setattr(module, "read_source_emission_block", read)
    monkeypatch.setattr(module.random, "sample", lambda seq, k: list(seq)[:k])
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=AsyncMock(return_value=_payout(a, 151)),
        _safe_rpc_error=str,
        _historical_substrate_urls=lambda: ["wss://archive.invalid"],
    )

    class _Connect:
        """Stands in for AsyncSubstrateInterface(url=...) as a context manager."""

        def __call__(self, url):
            del url
            return self

        async def __aenter__(self):
            return substrate

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr("async_substrate_interface.AsyncSubstrateInterface", _Connect())
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))
    # The whole sweep: the shadow runs after it, in a window of its own.
    await collector.sweep()
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        # The event planner's evidence drives the cursor whatever the shadow says.
        assert (cursor.block, cursor.block_hash) == (end, _hash(end))
        return cursor.reveal_plan_shadow


async def test_reveal_plan_shadow_agrees_when_it_plans_every_acted_block(
    app, session_maker, monkeypatch
):
    from ditto.api_server import source_emission_collector as module

    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    summary = await _shadowed_sweep(
        app,
        session_maker,
        monkeypatch,
        AsyncMock(return_value=_sparse_plan(100, end, (150, 151))),
    )
    assert summary["ranges"] == summary["agreed"] == 1
    assert summary["last_outcome"] == "agreed"
    assert summary["last_range"] == [101, end]
    assert summary["blocks"] == end - 100


async def test_reveal_plan_shadow_records_a_missed_reveal(
    app, session_maker, monkeypatch
):
    from ditto.api_server import source_emission_collector as module

    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    summary = await _shadowed_sweep(
        app,
        session_maker,
        monkeypatch,
        AsyncMock(return_value=_sparse_plan(100, end, (151,))),
    )
    assert summary["missed"] == 1 and summary.get("agreed", 0) == 0
    assert summary["recent_misses"] == [
        {"first_block": 101, "last_block": end, "blocks": [150]}
    ]


@pytest.mark.parametrize("failure", ["error", "fallback"])
async def test_reveal_plan_shadow_failure_never_blocks_the_sweep(
    app, session_maker, monkeypatch, failure
):
    from ditto.api_server import source_emission_collector as module
    from ditto.chain.source_emission_verifier import SourceEmissionRangePlan

    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    planner = (
        AsyncMock(side_effect=RuntimeError("archive throttled"))
        if failure == "error"
        else AsyncMock(
            return_value=SourceEmissionRangePlan(101, end, {}, (), True, None)
        )
    )
    summary = await _shadowed_sweep(app, session_maker, monkeypatch, planner)
    assert summary[failure] == 1 and summary["last_outcome"] == failure
    if failure == "error":
        assert "archive throttled" in summary["last_error"]


async def test_reveal_plan_shadow_runs_after_payouts_within_the_budget(
    app, session_maker, monkeypatch
):
    """The shadow runs after payout resolution; a spent window skips it."""
    from ditto.api_server import source_emission_collector as module

    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    order: list[str] = []

    async def shadow_plan(*_args, **_kwargs):
        order.append("shadow")
        return _sparse_plan(100, end, (150, 151))

    async def resolve(_substrate, **_kwargs):
        order.append("resolve")
        return 0

    original = module.SourceEmissionCollector._sweep_provider

    async def sweep_with(self, substrate, *, budget=None):
        monkeypatch.setattr(self, "resolve_pending_payouts", resolve)
        await original(self, substrate, budget=budget)

    monkeypatch.setattr(module.SourceEmissionCollector, "_sweep_provider", sweep_with)
    await _shadowed_sweep(app, session_maker, monkeypatch, shadow_plan)
    assert order[-2:] == ["resolve", "shadow"]

    # A window too short for a comparison skips it rather than racing it.
    collector = _collector(app, session_maker)
    collector._pending_shadow = (101, end, [150])
    planner = AsyncMock()
    monkeypatch.setattr(module, "plan_source_emission_range_by_reveals", planner)
    async with asyncio.timeout(5) as budget:
        await collector._run_pending_shadow(SimpleNamespace(), budget)
    planner.assert_not_awaited()
    assert collector._pending_shadow is None


async def test_primary_mode_plans_with_the_reveal_planner_only(
    app, session_maker, monkeypatch
):
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionCollectorCursor

    monkeypatch.setattr(module, "_REVEAL_PLAN_MODE", "primary")
    a, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    events = AsyncMock(side_effect=AssertionError("event planner must not run"))
    monkeypatch.setattr(module, "plan_source_emission_range", events)
    monkeypatch.setattr(
        module,
        "plan_source_emission_range_by_reveals",
        AsyncMock(return_value=_sparse_plan(100, end, (151,))),
    )

    async def read(_substrate, *, netuid, block, **_kwargs):
        del netuid
        return _observed(a, block, payout=block == 151)

    monkeypatch.setattr(module, "read_source_emission_block", read)
    monkeypatch.setattr(module.random, "sample", lambda seq, k: list(seq)[:k])
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=AsyncMock(return_value=_payout(a, 151)),
        _safe_rpc_error=str,
    )
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))
    await collector._sweep_provider(substrate)
    events.assert_not_awaited()
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == end and cursor.reveal_plan_shadow is None


async def test_sparse_audit_disagreement_fails_closed(app, session_maker, monkeypatch):
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionCollectorCursor

    a, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    monkeypatch.setattr(
        module,
        "plan_source_emission_range",
        AsyncMock(return_value=_sparse_plan(100, end, ())),
    )

    async def read(_substrate, *, netuid, block, **_kwargs):
        del netuid
        # The full reader sees a reveal the plan called quiet.
        return _observed(a, block, reveal=True)

    monkeypatch.setattr(module, "read_source_emission_block", read)
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))

    with pytest.raises(ValueError, match="audit disagreed"):
        await collector._sweep_provider(substrate)
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == 100


@pytest.mark.parametrize("plan_outcome", ["full_scan", "unavailable"])
async def test_sparse_catchup_falls_back_to_the_per_block_scan(
    app, session_maker, monkeypatch, plan_outcome
):
    from ditto.api_server import source_emission_collector as module

    a, substrate = await _deep_backlog(app, session_maker, 100)
    plan = (
        AsyncMock(
            return_value=_sparse_plan(
                100, 100 + module._SWEEP_SPARSE_BATCH_BLOCKS, (), full_scan=True
            )
        )
        if plan_outcome == "full_scan"
        else AsyncMock(side_effect=RuntimeError("RPC call is unsafe"))
    )
    monkeypatch.setattr(module, "plan_source_emission_range", plan)
    scanned = []

    async def read(_substrate, *, netuid, block, **_kwargs):
        del netuid
        scanned.append(block)
        return _observed(a, block)

    monkeypatch.setattr(module, "read_source_emission_block", read)
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))

    await collector._sweep_provider(substrate)

    assert scanned == list(range(101, 101 + module._SWEEP_CATCHUP_BATCH_BLOCKS))


async def test_sparse_plan_must_start_at_the_durable_cursor(
    app, session_maker, monkeypatch
):
    from ditto.api_server import source_emission_collector as module
    from ditto.chain.source_emission_verifier import SourceEmissionRangePlan

    _, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    hashes = {b: _hash(b) for b in range(100, end + 1)}
    hashes[100] = "0x" + "ee" * 32  # a different chain than the cursor's
    monkeypatch.setattr(
        module,
        "plan_source_emission_range",
        AsyncMock(
            return_value=SourceEmissionRangePlan(101, end, hashes, (), False, "runtime")
        ),
    )
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))

    with pytest.raises(ValueError, match="does not match the planned range"):
        await collector._sweep_provider(substrate)


async def test_primary_replay_reads_planned_blocks_concurrently_and_commits_in_order(
    app, session_maker, monkeypatch
):
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionCollectorCursor, SourceEmissionPayout

    monkeypatch.setattr(module, "_REVEAL_PLAN_MODE", "primary")
    a, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    planned = (151, 158, 511, 518)
    monkeypatch.setattr(
        module,
        "plan_source_emission_range_by_reveals",
        AsyncMock(return_value=_sparse_plan(100, end, planned)),
    )
    in_flight, peak = 0, 0
    all_started = asyncio.Event()
    started: set[int] = set()

    async def read(_substrate, *, netuid, block, **_kwargs):
        nonlocal in_flight, peak
        del netuid
        in_flight += 1
        peak = max(peak, in_flight)
        started.add(block)
        if set(planned) <= started:
            all_started.set()
        try:
            if block in planned:
                # No planned read finishes until all of them are under way.
                await asyncio.wait_for(all_started.wait(), 5)
            return _observed(a, block, payout=block in (151, 511))
        finally:
            in_flight -= 1

    monkeypatch.setattr(module, "read_source_emission_block", read)
    monkeypatch.setattr(module.random, "sample", lambda seq, k: list(seq)[:k])
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=AsyncMock(
            side_effect=lambda _netuid, *, payout_block, **_kw: _payout(a, payout_block)
        ),
        _safe_rpc_error=str,
    )
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))

    await collector._sweep_provider(substrate)

    assert peak == module._SPARSE_READ_CONCURRENCY
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == end
        payouts = (await session.scalars(select(SourceEmissionPayout.block))).all()
        assert sorted(payouts) == [151, 511]


async def test_primary_audit_failure_commits_nothing_and_stops_every_read(
    app, session_maker, monkeypatch
):
    from ditto.api_server import source_emission_collector as module
    from ditto.db.models import SourceEmissionCollectorCursor

    monkeypatch.setattr(module, "_REVEAL_PLAN_MODE", "primary")
    a, substrate = await _deep_backlog(app, session_maker, 100)
    end = 100 + module._SWEEP_SPARSE_BATCH_BLOCKS
    monkeypatch.setattr(
        module,
        "plan_source_emission_range_by_reveals",
        AsyncMock(return_value=_sparse_plan(100, end, (151, 511))),
    )
    hang = asyncio.Event()

    async def read(_substrate, *, netuid, block, **_kwargs):
        del netuid
        if block in (151, 511):
            await hang.wait()  # a planned read still running when the audit fails
        return _observed(a, block, reveal=True)

    monkeypatch.setattr(module, "read_source_emission_block", read)
    monkeypatch.setattr(module.random, "sample", lambda seq, k: list(seq)[:k])
    collector = _collector(app, session_maker)
    monkeypatch.setattr(collector, "resolve_pending_payouts", AsyncMock(return_value=0))

    with pytest.raises(ValueError, match="audit disagreed"):
        await collector._sweep_provider(substrate)
    assert asyncio.all_tasks() == {asyncio.current_task()}
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, a["netuid"])
        assert cursor.block == 100
