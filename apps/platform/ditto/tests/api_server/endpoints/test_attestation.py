"""End-to-end coverage for owner-link submission and the reviewer read.

Uses real sr25519 keypairs so the whole chain -- CLI payload format, wire
model, verification, the payment-record coldkey binding, storage, and the
reviewer read -- is exercised together.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.attestation import canonical_pair, link_message
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, AthReview, AthReviewAction, EvaluationPayment

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_URL = "/api/v1/attestations/owner-link"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _kp(uri: str) -> bittensor.Keypair:
    return bittensor.Keypair.create_from_uri(uri)


async def _seed_payment(
    maker: async_sessionmaker[AsyncSession], *, hotkey: str, coldkey: str
) -> None:
    """Give a hotkey a payment record, which is what binds it to a coldkey."""
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="seeded",
                sha256=uuid4().hex + uuid4().hex,
                status=AgentStatus.SCORED,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address="5Destination",
                timestamp=datetime.now(UTC),
            )
        )


def _proof(
    signer: bittensor.Keypair,
    *,
    hotkey_lo: str,
    hotkey_hi: str,
    side: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: str = "hotkey",
    netuid: int = 118,
) -> dict:
    return {
        "key_kind": key_kind,
        "signer": signer.ss58_address,
        "signature": signer.sign(
            link_message(
                netuid=netuid,
                hotkey_lo=hotkey_lo,
                hotkey_hi=hotkey_hi,
                nonce=nonce,
                issued_at=issued_at,
                side=side,  # type: ignore[arg-type]
                key_kind=key_kind,  # type: ignore[arg-type]
                signer=signer.ss58_address,
            )
        ).hex(),
    }


def _body(
    a: bittensor.Keypair,
    b: bittensor.Keypair,
    *,
    netuid: int = 118,
    nonce: UUID | None = None,
    issued_at: datetime | None = None,
) -> dict:
    """A hotkey-proved link, with the pair passed in arbitrary order."""
    nonce = nonce or uuid4()
    issued_at = issued_at or datetime.now(UTC)
    lo, hi = canonical_pair(a.ss58_address, b.ss58_address)
    a_side = "lo" if a.ss58_address == lo else "hi"
    b_side = "hi" if a_side == "lo" else "lo"
    return {
        "netuid": netuid,
        "hotkey_a": a.ss58_address,
        "hotkey_b": b.ss58_address,
        "nonce": str(nonce),
        "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "proof_a": _proof(
            a,
            hotkey_lo=lo,
            hotkey_hi=hi,
            side=a_side,
            nonce=nonce,
            issued_at=issued_at,
            netuid=netuid,
        ),
        "proof_b": _proof(
            b,
            hotkey_lo=lo,
            hotkey_hi=hi,
            side=b_side,
            nonce=nonce,
            issued_at=issued_at,
            netuid=netuid,
        ),
    }


async def test_valid_link_is_recorded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice, bob = _kp("//Alice"), _kp("//Bob")

    response = await client.post(_URL, json=_body(alice, bob))

    assert response.status_code == 201, response.text
    payload = response.json()
    lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
    assert payload["hotkey_lo"] == lo
    assert payload["hotkey_hi"] == hi
    assert payload["evidence_grade"] == "hotkey-hotkey"
    # The scope is stated on the wire so nobody downstream has to infer it.
    assert payload["scope"] == "plagiarism-screening-only"
    assert payload["grants_additional_emission_slot"] is False
    assert payload["cleared_copy_review_count"] == 0


async def test_valid_link_backfills_pending_direct_pair_copy_review(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice, bob = _kp("//Alice"), _kp("//Bob")
    reference_id, held_id, review_id = uuid4(), uuid4(), uuid4()
    reason = f"content near-duplicate of agent {reference_id}"
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                Agent(
                    agent_id=reference_id,
                    miner_hotkey=alice.ss58_address,
                    name="reference",
                    sha256="aa" * 32,
                    status=AgentStatus.SCORED,
                    created_at=now - timedelta(minutes=2),
                ),
                Agent(
                    agent_id=held_id,
                    miner_hotkey=bob.ss58_address,
                    name="held",
                    sha256="bb" * 32,
                    status=AgentStatus.ATH_PENDING_REVIEW,
                    duplicate_of=reference_id,
                    review_reason=reason,
                    created_at=now - timedelta(minutes=1),
                ),
                AthReview(
                    review_id=review_id,
                    agent_id=held_id,
                    status="pending",
                    opened_at=now,
                    original_duplicate_of=reference_id,
                    original_reason=reason,
                    original_policy_version=9,
                    original_evidence={},
                    algorithm_provenance={"review_kind": "copy"},
                ),
            ]
        )

    response = await client.post(_URL, json=_body(alice, bob))

    assert response.status_code == 201, response.text
    assert response.json()["cleared_copy_review_count"] == 1
    async with session_maker() as session:
        held = await session.get(Agent, held_id)
        review = await session.get(AthReview, review_id)
        action = await session.scalar(
            select(AthReviewAction).where(AthReviewAction.review_id == review_id)
        )
        assert held is not None and held.status == AgentStatus.SCORED
        assert held.duplicate_of is None
        assert held.review_reason is None
        assert review is not None and review.status == "resolved"
        assert review.resolution == "clear"
        assert action is not None
        assert action.action == "clear"
        assert (
            action.evidence["owner_attestation_id"] == response.json()["attestation_id"]
        )


async def test_pair_order_does_not_matter(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The link is symmetric; submitting the pair reversed is the same link."""
    _install(app, session_maker)
    alice, bob = _kp("//Alice"), _kp("//Bob")

    first = await client.post(_URL, json=_body(alice, bob))
    assert first.status_code == 201

    reversed_pair = await client.post(_URL, json=_body(bob, alice))
    assert reversed_pair.status_code == 409
    assert "already links" in reversed_pair.text


async def test_coldkey_proved_half_is_accepted(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A miner who lost the old hotkey's key can still prove it via coldkey.

    The coldkey is checked against the payment record, which the platform
    learned from an on-chain payment proof -- not from this request.
    """
    _install(app, session_maker)
    alice, bob, cold = _kp("//Alice"), _kp("//Bob"), _kp("//ColdOwner")
    await _seed_payment(
        session_maker, hotkey=alice.ss58_address, coldkey=cold.ss58_address
    )

    lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
    alice_side = "lo" if alice.ss58_address == lo else "hi"
    bob_side = "hi" if alice_side == "lo" else "lo"
    nonce, issued_at = uuid4(), datetime.now(UTC)

    response = await client.post(
        _URL,
        json={
            "netuid": 118,
            "hotkey_a": alice.ss58_address,
            "hotkey_b": bob.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            # Alice's half signed by her COLDKEY, not her hotkey.
            "proof_a": _proof(
                cold,
                hotkey_lo=lo,
                hotkey_hi=hi,
                side=alice_side,
                nonce=nonce,
                issued_at=issued_at,
                key_kind="coldkey",
            ),
            "proof_b": _proof(
                bob,
                hotkey_lo=lo,
                hotkey_hi=hi,
                side=bob_side,
                nonce=nonce,
                issued_at=issued_at,
            ),
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["evidence_grade"] == "mixed"


async def test_coldkey_half_without_a_payment_binding_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice, bob, cold = _kp("//Alice"), _kp("//Bob"), _kp("//ColdOwner")
    lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
    alice_side = "lo" if alice.ss58_address == lo else "hi"
    bob_side = "hi" if alice_side == "lo" else "lo"
    nonce, issued_at = uuid4(), datetime.now(UTC)

    response = await client.post(
        _URL,
        json={
            "netuid": 118,
            "hotkey_a": alice.ss58_address,
            "hotkey_b": bob.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "proof_a": _proof(
                cold,
                hotkey_lo=lo,
                hotkey_hi=hi,
                side=alice_side,
                nonce=nonce,
                issued_at=issued_at,
                key_kind="coldkey",
            ),
            "proof_b": _proof(
                bob,
                hotkey_lo=lo,
                hotkey_hi=hi,
                side=bob_side,
                nonce=nonce,
                issued_at=issued_at,
            ),
        },
    )

    assert response.status_code == 400
    assert "no payment record" in response.text


async def test_forged_half_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice, bob, eve = _kp("//Alice"), _kp("//Bob"), _kp("//Eve")
    body = _body(alice, bob)
    lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
    a_side = "lo" if alice.ss58_address == lo else "hi"
    # Eve signs Alice's half but declares Alice as the signer.
    body["proof_a"]["signature"] = eve.sign(
        link_message(
            netuid=118,
            hotkey_lo=lo,
            hotkey_hi=hi,
            nonce=UUID(body["nonce"]),
            issued_at=datetime.fromisoformat(body["issued_at"]),
            side=a_side,  # type: ignore[arg-type]
            key_kind="hotkey",
            signer=alice.ss58_address,
        )
    ).hex()

    response = await client.post(_URL, json=body)

    assert response.status_code == 400
    assert "did not verify" in response.text


async def test_link_at_a_third_party_hotkey_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The load-bearing abuse case, end to end.

    Mallory tries to mint a link naming a victim's hotkey using only her own
    key. Requiring both halves means the link never forms, so it never
    pollutes the victim's reviewer surface or their screening.
    """
    _install(app, session_maker)
    mallory, victim = _kp("//Mallory"), _kp("//Victim")
    body = _body(mallory, victim)
    lo, hi = canonical_pair(mallory.ss58_address, victim.ss58_address)
    victim_side = "lo" if victim.ss58_address == lo else "hi"
    # Mallory signs the victim's half with her own key.
    body["proof_b"]["signature"] = mallory.sign(
        link_message(
            netuid=118,
            hotkey_lo=lo,
            hotkey_hi=hi,
            nonce=UUID(body["nonce"]),
            issued_at=datetime.fromisoformat(body["issued_at"]),
            side=victim_side,  # type: ignore[arg-type]
            key_kind="hotkey",
            signer=victim.ss58_address,
        )
    ).hex()

    response = await client.post(_URL, json=body)

    assert response.status_code == 400
    assert "did not verify" in response.text


async def test_replayed_attestation_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The same signed payload submitted twice is refused on the nonce."""
    _install(app, session_maker)
    body = _body(_kp("//Alice"), _kp("//Bob"))

    first = await client.post(_URL, json=body)
    assert first.status_code == 201

    replay = await client.post(_URL, json=body)
    assert replay.status_code == 409
    assert "nonce" in replay.text


async def test_expired_attestation_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    stale = datetime.now(UTC) - timedelta(days=3)
    body = _body(_kp("//Alice"), _kp("//Bob"), issued_at=stale)

    response = await client.post(_URL, json=body)

    assert response.status_code == 400
    assert "expired" in response.text


async def test_wrong_netuid_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    body = _body(_kp("//Alice"), _kp("//Bob"), netuid=64)

    response = await client.post(_URL, json=body)

    assert response.status_code == 400
    assert "netuid" in response.text


async def test_reviewer_read_is_symmetric(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice, bob = _kp("//Alice"), _kp("//Bob")
    assert (await client.post(_URL, json=_body(alice, bob))).status_code == 201

    for me, them in ((alice, bob), (bob, alice)):
        response = await client.get(
            f"/api/v1/admin/owner-attestations/{me.ss58_address}", headers=_HEADERS
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["linkage_basis"] == "signed_owner_attestation"
        assert [link["hotkey"] for link in payload["linked_hotkeys"]] == [
            them.ss58_address
        ]
        assert payload["attestations"][0]["counterparty"] == them.ss58_address
        assert payload["attestations"][0]["active"] is True
        assert payload["attestations"][0]["evidence_grade"] == "hotkey-hotkey"
        assert "emission" in payload["scope_caveat"]


async def test_reviewer_read_requires_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.get("/api/v1/admin/owner-attestations/5Whoever")
    assert response.status_code in (401, 403)


async def test_unknown_hotkey_reads_as_empty_not_404(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """ "This miner never attested anything" is an answer a reviewer needs."""
    _install(app, session_maker)
    response = await client.get(
        "/api/v1/admin/owner-attestations/5NeverSeen", headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["attestations"] == []
    assert response.json()["linked_hotkeys"] == []


async def test_revocation_deactivates_the_link_but_keeps_the_row(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Revocation is prospective and auditable.

    The link stops resolving, so future screening no longer exempts it, but the
    row survives so a reviewer can still answer "was it live back then".
    """
    _install(app, session_maker)
    alice, bob = _kp("//Alice"), _kp("//Bob")
    created = await client.post(_URL, json=_body(alice, bob))
    attestation_id = created.json()["attestation_id"]

    revoked = await client.post(
        f"/api/v1/admin/owner-attestations/{attestation_id}/revoke",
        json={"reason": "one key was sold to another operator"},
        headers=_HEADERS,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["active"] is False

    read = await client.get(
        f"/api/v1/admin/owner-attestations/{alice.ss58_address}", headers=_HEADERS
    )
    payload = read.json()
    # No longer confers an exemption...
    assert payload["linked_hotkeys"] == []
    # ...but the history is intact.
    assert len(payload["attestations"]) == 1
    assert payload["attestations"][0]["active"] is False
    assert payload["attestations"][0]["revoked_reason"] == (
        "one key was sold to another operator"
    )


async def test_revoking_an_unknown_attestation_is_404(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.post(
        f"/api/v1/admin/owner-attestations/{uuid4()}/revoke",
        json={"reason": "no such link exists"},
        headers=_HEADERS,
    )
    assert response.status_code == 404


async def test_revocation_requires_an_admin_actor(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    created = await client.post(_URL, json=_body(_kp("//Alice"), _kp("//Bob")))
    response = await client.post(
        f"/api/v1/admin/owner-attestations/{created.json()['attestation_id']}/revoke",
        json={"reason": "missing the actor header"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert response.status_code == 422
