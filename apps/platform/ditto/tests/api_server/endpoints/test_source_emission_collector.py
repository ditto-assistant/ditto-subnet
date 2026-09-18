"""Signed claims through durable reveal/payout attribution and source embargo."""

from __future__ import annotations

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
