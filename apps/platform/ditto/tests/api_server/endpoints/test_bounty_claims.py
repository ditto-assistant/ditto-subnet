"""Paid bounty claims: signed holds, simulated fees and refund-on-loss (#2942, P2).

Real PostgreSQL, real sr25519 signatures, the real GitHub link flow (with a
fake provider) and the real CLA signing flow. Only the chain is mocked.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.bounty import (
    BountyClaimRequest,
    BountyFeeReceipt,
    bounty_claim_message,
    bounty_team_digest,
    simulated_fee_block_hash,
)
from ditto.api_server import bounty_claims
from ditto.api_server.bounty_claims import BountyFeeRejected, VerifiedBountyFee
from ditto.chain import ChainConnectionError
from ditto.db.models import (
    BountyActionNonce,
    BountyClaim,
    BountyClaimControl,
    BountyFeePayment,
    BountyObligation,
    BountySlot,
)
from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
    _HEADERS as _ADMIN_TOKEN,
)
from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
    _install as admin_install,
)
from ditto.tests.api_server.endpoints.test_bounties import TERMS
from ditto.tests.api_server.endpoints.test_bounties import publish as publish_bounty
from ditto.tests.api_server.endpoints.test_contributor_cla import (
    publish as publish_cla,
)
from ditto.tests.api_server.endpoints.test_contributor_cla import signed as signed_cla
from ditto.tests.api_server.endpoints.test_miner_ditto_link import _sign_in
from ditto.tests.api_server.endpoints.test_miner_github_link import (
    browser_flow,
    signed_link,
)
from ditto.tests.api_server.endpoints.test_miner_github_link import (
    setup as github_setup,
)

pytestmark = pytest.mark.asyncio
_ACTOR = "operator-fixture@example.com"
_HEADERS = {**_ADMIN_TOKEN, "X-Admin-Actor": _ACTOR}
REPO = "ditto-assistant/ditto-subnet"
ISSUE = 2942
CLAIMS = "/api/v1/bounties/claims"


@dataclass(frozen=True)
class Person:
    hotkey: bittensor.Keypair
    coldkey: bittensor.Keypair
    login: str
    user_id: int


def person(name: str, user_id: int) -> Person:
    return Person(
        hotkey=bittensor.Keypair.create_from_uri(f"//{name}"),
        coldkey=bittensor.Keypair.create_from_uri(f"//{name}//cold"),
        login=f"{name.lower()}-dev",
        user_id=user_id,
    )


ALICE, BOB, CAROL, DAVE = (
    person("Alice", 101),
    person("Bob", 102),
    person("Carol", 103),
    person("Dave", 104),
)


class World:
    """The app, its GitHub fake, chain owners and the published bounty."""

    def __init__(self, app, client, maker):
        self.app, self.client, self.maker = app, client, maker
        self.provider = github_setup(app, maker)
        admin_install(app, maker)
        app.state.session_maker = maker
        self.owners: dict[str, str] = {}
        app.state.chain.get_coldkey_for_hotkey = AsyncMock(
            side_effect=lambda hotkey, *_args: self.owners[hotkey]
        )
        self.detail: dict = {}

    async def link(self, who: Person, *, sign_cla: bool = True) -> None:
        self.owners[who.hotkey.ss58_address] = who.coldkey.ss58_address
        self.provider.user_id, self.provider.login = who.user_id, who.login
        self.provider.accounts = {**self.provider.accounts, who.login: who.user_id}
        token = await _sign_in(self.client, who.hotkey)
        started, _, _ = await browser_flow(self.client, token)
        linked = await self.client.post(
            "/api/v1/me/github-link/confirm",
            headers={"authorization": f"Bearer {token}"},
            json=signed_link(
                who.hotkey, started["attempt_id"], user_id=who.user_id, login=who.login
            ),
        )
        assert linked.status_code == 200, linked.text
        if sign_cla:
            await self.sign_cla(who)

    async def sign_cla(self, who: Person) -> None:
        signed = await self.client.post(
            "/api/v1/cla/sign", json=signed_cla(who.hotkey, self.agreement)
        )
        assert signed.status_code == 200, signed.text

    async def start(self, *people: Person, **terms) -> None:
        self.agreement = await publish_cla(self.client)
        for who in people:
            await self.link(who)
        published = await publish_bounty(self.client, issue=ISSUE, terms=terms)
        assert published.status_code == 200, published.text
        await self.refresh()

    async def refresh(self) -> dict:
        board = (await self.client.get("/api/v1/public/bounties")).json()
        bounty_id = board["items"][0]["bounty_id"]
        self.detail = (
            await self.client.get(f"/api/v1/public/bounties/{bounty_id}")
        ).json()
        return self.detail

    def body(
        self,
        who: Person,
        *,
        kind: str = "hotkey",
        signer: bittensor.Keypair | None = None,
        issued: datetime | None = None,
        nonce: UUID | None = None,
        **over,
    ) -> dict:
        summary = self.detail["summary"]
        payee = over.get("payee_coldkey", who.coldkey.ss58_address)
        login = over.get("github_login", who.login)
        fields = {
            "netuid": 118,
            "repo": REPO,
            "issue": ISSUE,
            "revision": summary["current_revision"],
            "spec_digest": summary["spec_digest"],
            "hotkey": who.hotkey.ss58_address,
            "payee_coldkey": payee,
            "github_login": login,
            "team_digest": bounty_team_digest(
                payee_coldkey=payee, members=[(who.hotkey.ss58_address, login)]
            ),
            "nonce": nonce or uuid4(),
            "issued_at": issued or datetime.now(UTC),
        }
        fields.update(over)
        key = signer or (who.coldkey if kind == "coldkey" else who.hotkey)
        payload = bounty_claim_message(**fields, key_kind=kind, signer=key.ss58_address)
        return {
            **{k: str(v) if isinstance(v, UUID) else v for k, v in fields.items()},
            "issued_at": fields["issued_at"].isoformat(),
            "proof": {
                "key_kind": kind,
                "signer": key.ss58_address,
                "signature": key.sign(payload).hex(),
            },
        }

    async def hold(self, who: Person, **kwargs):
        return await self.client.post(CLAIMS, json=self.body(who, **kwargs))

    async def confirm(self, claim: dict, receipt: dict | None = None):
        return await self.client.post(
            f"{CLAIMS}/{claim['claim_id']}/confirm",
            json={"receipt": receipt or claim["simulated_receipt"]},
        )

    async def counts(self) -> dict[str, int]:
        async with self.maker() as session:
            return {
                model.__tablename__: int(
                    await session.scalar(select(func.count()).select_from(model))
                )
                for model in (
                    BountyActionNonce,
                    BountyClaim,
                    BountyFeePayment,
                    BountyObligation,
                    BountyClaimControl,
                )
            }


@pytest.fixture
async def world(app, client, session_maker):
    return World(app, client, session_maker)


def travel(monkeypatch, delta: timedelta) -> None:
    """Move the clock the claim engine and the public reads share."""
    monkeypatch.setattr(bounty_claims, "utcnow", lambda: datetime.now(UTC) + delta)


async def test_claim_holds_the_lowest_open_slot_then_the_fee_claims_it(world):
    await world.start(ALICE)
    before = datetime.now(UTC)
    body = world.body(ALICE)
    held = await world.client.post(CLAIMS, json=body)
    assert held.status_code == 200, held.text
    claim = held.json()
    assert claim["state"] == "held" and claim["slot_status"] == "held"
    assert (claim["round"], claim["position"], claim["revision"]) == (1, 1, 1)
    assert claim["payee_coldkey"] == ALICE.coldkey.ss58_address
    assert claim["fee_alpha_rao"] == TERMS["entry_fee_alpha_rao"]
    assert claim["fee_escrow_coldkey"] == TERMS["fee_escrow_coldkey"]
    expires = datetime.fromisoformat(claim["hold_expires_at"])
    assert timedelta(minutes=30) <= expires - before <= timedelta(minutes=31)
    assert claim["simulated_receipt"] == {
        "block_hash": simulated_fee_block_hash(
            UUID(claim["claim_id"]), body["proof"]["signature"]
        ),
        "block_number": 0,
        "extrinsic_index": 0,
    }
    detail = await world.refresh()
    assert detail["summary"]["slots"]["held"] == 1
    assert detail["summary"]["slots"]["open"] == 2
    first = detail["slots"][0]
    assert first["status"] == "held" and first["claimant"] == {
        "claim_id": claim["claim_id"],
        "hotkey": ALICE.hotkey.ss58_address,
        "payee_coldkey": ALICE.coldkey.ss58_address,
        "github_login": ALICE.login,
        "revision": 1,
    }

    confirmed = await world.confirm(claim)
    assert confirmed.status_code == 200, confirmed.text
    paid = confirmed.json()
    assert paid["state"] == "claimed" and paid["slot_status"] == "claimed"
    deadline = datetime.fromisoformat(paid["deadline_at"])
    claimed_at = datetime.fromisoformat(paid["claimed_at"])
    assert deadline - claimed_at == timedelta(days=5)
    assert paid["receipts"] == [claim["simulated_receipt"]] and paid["refunds"] == []
    # The same receipt again is the recorded outcome, never a second charge.
    again = await world.confirm(claim)
    assert again.status_code == 200 and again.json() == paid
    read = await world.client.get(f"{CLAIMS}/{claim['claim_id']}")
    assert read.status_code == 200 and read.json() == paid
    assert read.headers["cache-control"] == "no-store"
    detail = await world.refresh()
    assert detail["summary"]["slots"]["claimed"] == 1
    assert detail["slots"][0]["deadline_at"] == paid["deadline_at"]
    assert [e["kind"] for e in detail["events"]][-2:] == ["slot_held", "slot_claimed"]
    async with world.maker() as session:
        payment = await session.scalar(select(BountyFeePayment))
        assert (payment.outcome, payment.simulated) == ("allocated", True)


async def test_an_exact_retry_of_a_hold_returns_the_original(world):
    await world.start(ALICE)
    body = world.body(ALICE)
    first = await world.client.post(CLAIMS, json=body)
    again = await world.client.post(CLAIMS, json=body)
    assert first.status_code == again.status_code == 200
    assert again.json()["claim_id"] == first.json()["claim_id"]
    # Any other request with the same nonce is a replay.
    reused = await world.hold(ALICE, nonce=UUID(body["nonce"]))
    assert reused.status_code == 409 and "nonce" in reused.text
    assert (await world.counts())["bounty_claims"] == 1


async def test_concurrent_identical_holds_record_one_claim(world):
    await world.start(ALICE)
    body = world.body(ALICE)
    results = await asyncio.gather(
        *(world.client.post(CLAIMS, json=body) for _ in range(3))
    )
    assert {r.status_code for r in results} == {200}
    assert len({r.json()["claim_id"] for r in results}) == 1
    assert (await world.counts())["bounty_claims"] == 1


async def test_a_hold_that_loses_its_nonce_race_reads_the_winner_now(
    world, monkeypatch
):
    await world.start(ALICE)
    body = world.body(ALICE)
    first = await world.client.post(CLAIMS, json=body)
    assert first.status_code == 200, first.text
    base, late, lookups = datetime.now(UTC), False, 0
    real_retry, real_violation = bounty_claims._retry_of, bounty_claims.unique_violation

    async def racing_retry(session, request, *, now):
        # The first lookup runs before the winner commits, so finds nothing.
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else await real_retry(session, request, now=now)

    def violation(exc):
        # The winner's hold lapses while this request handles the conflict.
        nonlocal late
        late = True
        return real_violation(exc)

    monkeypatch.setattr(bounty_claims, "_retry_of", racing_retry)
    monkeypatch.setattr(bounty_claims, "unique_violation", violation)
    view = await bounty_claims.hold_claim(
        world.maker,
        chain=world.app.state.chain,
        github=world.app.state.github_link,
        body=BountyClaimRequest.model_validate(body),
        netuid=118,
        clock=lambda: base + (timedelta(minutes=31) if late else timedelta()),
    )
    assert (lookups, view.claim_id) == (2, UUID(first.json()["claim_id"]))
    assert view.state == "hold_expired"


async def test_three_slots_and_the_last_one_has_one_winner(world):
    await world.start(ALICE, BOB, CAROL, DAVE)
    for who in (ALICE, BOB):
        assert (await world.hold(who)).status_code == 200
    results = await asyncio.gather(world.hold(CAROL), world.hold(DAVE))
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert "every slot in round 1 is taken" in loser.text
    detail = await world.refresh()
    assert [s["status"] for s in detail["slots"]] == ["held"] * 3
    assert detail["summary"]["slots"]["held"] == 3


async def test_one_live_slot_per_github_account_and_payee(world):
    await world.start(ALICE, BOB)
    assert (await world.hold(ALICE)).status_code == 200
    again = await world.hold(ALICE)
    assert again.status_code == 409 and "already holds a slot" in again.text
    # Another hotkey paying to Alice's coldkey is the same payee.
    world.owners[BOB.hotkey.ss58_address] = ALICE.coldkey.ss58_address
    shared = await world.hold(BOB, payee_coldkey=ALICE.coldkey.ss58_address)
    assert shared.status_code == 409 and "already holds a slot" in shared.text
    assert (await world.counts())["bounty_claims"] == 1


async def test_a_coldkey_proof_is_signed_by_the_payee(world):
    await world.start(ALICE)
    held = await world.hold(ALICE, kind="coldkey")
    assert held.status_code == 200, held.text
    forged = await world.hold(ALICE, kind="coldkey", signer=BOB.coldkey)
    assert forged.status_code == 400 and "payee coldkey" in forged.text


async def test_the_stored_claim_reverifies_from_its_own_fields(world):
    await world.start(ALICE)
    # GitHub logins are case-insensitive; the record keeps the signed spelling.
    held = await world.hold(ALICE, github_login=ALICE.login.upper())
    assert held.status_code == 200, held.text
    async with world.maker() as session:
        claim = await session.get(BountyClaim, UUID(held.json()["claim_id"]))
    assert claim.github_login == ALICE.login.upper()
    payload = bounty_claim_message(
        netuid=claim.netuid,
        repo=REPO,
        issue=ISSUE,
        revision=claim.revision,
        spec_digest=claim.spec_digest,
        hotkey=claim.hotkey,
        payee_coldkey=claim.payee_coldkey,
        github_login=claim.github_login,
        team_digest=claim.team_digest,
        nonce=claim.nonce,
        issued_at=claim.issued_at,
        key_kind=claim.key_kind,
        signer=claim.signer,
    )
    assert ALICE.hotkey.verify(payload, bytes.fromhex(claim.signature))
    assert claim.github_user_id == ALICE.user_id


@pytest.mark.parametrize(
    "case,status,message",
    [
        ("netuid", 400, "netuid"),
        ("team", 400, "solo"),
        ("signature", 400, "did not verify"),
        ("stale", 400, "expired"),
        ("revision", 409, "terms changed"),
        ("digest", 409, "terms changed"),
        ("unknown", 404, "bounty not found"),
        ("payee", 403, "Owner(hotkey)"),
        ("unlinked", 403, "active verified link"),
        ("login", 403, "active verified link"),
        ("unsigned", 403, "Contributor agreement v1 is required"),
        ("github", 503, "retry before claiming"),
        ("chain", 503, "finalized ownership unavailable"),
    ],
)
async def test_refused_claims_write_nothing(world, case, status, message):
    await world.start(ALICE)
    await world.link(BOB, sign_cla=False)
    who, over, body = ALICE, {}, None
    if case == "netuid":
        over = {"netuid": 119}
    elif case == "team":
        over = {"team_digest": "00" * 32}
    elif case == "stale":
        over = {"issued_at": datetime.now(UTC) - timedelta(days=2)}
    elif case == "revision":
        over = {"revision": 2}
    elif case == "digest":
        over = {"spec_digest": "00" * 32}
    elif case == "unknown":
        over = {"issue": 9999}
    elif case == "payee":
        over = {"payee_coldkey": BOB.coldkey.ss58_address}
    elif case == "unlinked":
        who = person("Eve", 105)
        world.owners[who.hotkey.ss58_address] = who.coldkey.ss58_address
    elif case == "login":
        over = {"github_login": BOB.login}
    elif case == "unsigned":
        who = BOB
    elif case == "github":
        world.provider.users_reply = lambda: httpx.Response(503)
    elif case == "chain":
        world.app.state.chain.get_finalized_block = AsyncMock(
            side_effect=ChainConnectionError("fixture outage")
        )
    body = world.body(who, **over)
    if case == "signature":
        body["proof"]["signature"] = "00" * 64
    refused = await world.client.post(CLAIMS, json=body)
    assert refused.status_code == status, refused.text
    assert message in refused.json()["message"]
    assert (await world.counts())["bounty_claims"] == 0
    assert (await world.counts())["bounty_action_nonces"] == 0


async def test_claims_need_the_cla_even_with_the_upload_gate_off(world):
    # The upload gate is seeded off; the bounty guard still refuses.
    await world.start(ALICE)
    await world.link(BOB, sign_cla=False)
    refused = await world.hold(BOB)
    assert refused.status_code == 403 and "cla sign" in refused.text
    await world.sign_cla(BOB)
    assert (await world.hold(BOB)).status_code == 200


async def test_a_lapsed_hold_reads_open_and_its_late_fee_is_refunded(
    world, monkeypatch
):
    await world.start(ALICE, BOB)
    claim = (await world.hold(ALICE)).json()
    travel(monkeypatch, timedelta(minutes=31))
    # Public reads never write: past the hold the slot reads open at once.
    detail = await world.refresh()
    async with world.maker() as session:
        assert (await session.get(BountySlot, UUID(claim["slot_id"]))).status == "held"
    assert detail["summary"]["slots"]["open"] == 3
    assert detail["slots"][0]["status"] == "open"
    assert detail["slots"][0]["claimant"] is None
    assert detail["slots"][0]["status_changed_at"] == claim["hold_expires_at"]
    # The claim's state is derived, so the lapsed, unpaid hold reads expired
    # without any write to the claim.
    lapsed = (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()
    assert (lapsed["state"], lapsed["slot_status"]) == ("hold_expired", "open")
    late = await world.confirm(claim)
    assert late.status_code == 200, late.text
    refund = late.json()
    assert refund["state"] == "refund_owed" and refund["slot_status"] == "open"
    assert [r["reason"] for r in refund["refunds"]] == ["hold_expired"]
    assert refund["refunds"][0]["amount_alpha_rao"] == TERMS["entry_fee_alpha_rao"]
    assert refund["refunds"][0]["payee_coldkey"] == ALICE.coldkey.ss58_address
    assert refund["refunds"][0]["simulated"] is True
    # The release is stamped at the hold's end, not when it was noticed.
    events = (await world.refresh())["events"]
    expired = next(e for e in events if e["kind"] == "hold_expired")
    assert expired["recorded_at"] == claim["hold_expires_at"]
    assert events[-1]["kind"] == "fee_refund_owed"
    # Resending the receipt returns the refund; it is never charged twice.
    again = await world.confirm(claim)
    assert again.status_code == 200 and again.json() == refund
    assert (await world.counts())["bounty_obligations"] == 1
    # The reopened slot is free for the next contributor.
    taken = await world.hold(BOB)
    assert taken.status_code == 200 and taken.json()["position"] == 1
    # Bob's hold is his own; Alice's claim still reads its released slot.
    mine = (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()
    assert (mine["state"], mine["slot_status"]) == ("refund_owed", "open")


async def test_exact_hold_boundary(world, monkeypatch):
    await world.start(ALICE, BOB)
    claim = (await world.hold(ALICE)).json()
    expires = datetime.fromisoformat(claim["hold_expires_at"])
    # One microsecond before the end the hold still wins its slot.
    monkeypatch.setattr(
        bounty_claims, "utcnow", lambda: expires - timedelta(microseconds=1)
    )
    assert (await world.confirm(claim)).json()["state"] == "claimed"
    other = (await world.hold(BOB)).json()
    ends = datetime.fromisoformat(other["hold_expires_at"])
    # At the end exactly, it has lapsed.
    monkeypatch.setattr(bounty_claims, "utcnow", lambda: ends)
    late = (await world.confirm(other)).json()
    assert late["state"] == "refund_owed"
    assert late["refunds"][0]["reason"] == "hold_expired"


async def test_the_hold_starts_after_slow_verification(world, monkeypatch):
    await world.start(ALICE)
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(bounty_claims, "utcnow", lambda: clock[0])
    owner = world.app.state.chain.get_coldkey_for_hotkey.side_effect

    def slow_owner(hotkey, *args):
        clock[0] += timedelta(minutes=20)
        return owner(hotkey, *args)

    world.app.state.chain.get_coldkey_for_hotkey.side_effect = slow_owner
    claim = (await world.hold(ALICE)).json()
    assert claim["state"] == "held"
    # The full hold runs from after the chain call, not from the request.
    assert datetime.fromisoformat(claim["hold_expires_at"]) == clock[0] + timedelta(
        minutes=30
    )


async def test_a_lock_wait_past_the_hold_lapses_it(world, monkeypatch):
    await world.start(ALICE)
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(bounty_claims, "utcnow", lambda: clock[0])
    claim = (await world.hold(ALICE)).json()
    locked = bounty_claims.locked_bounty

    async def slow_lock(session, bounty_id):
        held = await locked(session, bounty_id)
        clock[0] += timedelta(minutes=31)  # another transaction held the row
        return held

    monkeypatch.setattr(bounty_claims, "locked_bounty", slow_lock)
    late = (await world.confirm(claim)).json()
    assert late["state"] == "refund_owed"
    assert late["refunds"][0]["reason"] == "hold_expired"


async def test_a_slot_lost_without_lapse_is_refunded(world):
    await world.start(ALICE)
    claim = (await world.hold(ALICE)).json()
    # A slot leaves the claim by another path (P3 cancellations do this).
    async with world.maker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE bounty_slots SET status = 'cancelled', claim_id = NULL "
                "WHERE slot_id = :slot"
            ),
            {"slot": claim["slot_id"]},
        )
    lost = (await world.confirm(claim)).json()
    assert lost["state"] == "refund_owed"
    assert lost["refunds"][0]["reason"] == "slot_lost"


@pytest.mark.parametrize("change", ["owner", "unlinked", "cla", "cla_resigned"])
async def test_an_identity_change_before_payment_refunds_and_releases(world, change):
    await world.start(ALICE, BOB)
    claim = (await world.hold(ALICE)).json()
    if change == "owner":
        world.owners[ALICE.hotkey.ss58_address] = BOB.coldkey.ss58_address
    elif change == "unlinked":
        # An operator revokes the link through the audited path.
        link = (
            await world.client.get(
                f"/api/v1/public/github-link/{ALICE.hotkey.ss58_address}"
            )
        ).json()["link"]
        route = f"/api/v1/admin/github-links/{link['link_id']}"
        control = (await world.client.get(route, headers=_HEADERS)).json()
        revoked = await world.client.post(
            f"{route}/revoke",
            headers=_HEADERS,
            json={
                "reason": "fixture hostile link",
                "confirmation": control["confirmation"],
            },
        )
        assert revoked.status_code == 200, revoked.text
    else:
        # A newer agreement takes effect before the fee arrives; signing it
        # is still not the consent the hold was made under.
        world.agreement = await publish_cla(world.client, 2)
        if change == "cla_resigned":
            await world.sign_cla(ALICE)
    changed = (await world.confirm(claim)).json()
    assert changed["state"] == "refund_owed"
    assert changed["refunds"][0]["reason"] == "identity_changed"
    detail = await world.refresh()
    assert detail["slots"][0]["status"] == "open"
    assert detail["events"][-2]["kind"] == "hold_released"


@pytest.mark.parametrize("namespace", ["github-link", "cla-policy"])
async def test_confirmation_waits_for_an_identity_write_in_flight(world, namespace):
    await world.start(ALICE)
    claim = (await world.hold(ALICE)).json()
    async with world.maker() as writer, writer.begin():
        # An unlink, revocation or CLA publication holds its lock exclusively.
        await writer.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:ns, 0))"),
            {"ns": namespace},
        )
        confirming = asyncio.create_task(world.confirm(claim))
        await asyncio.sleep(0.5)
        assert not confirming.done()
    confirmed = (await confirming).json()
    assert confirmed["state"] == "claimed"


async def test_claims_share_the_link_lock_with_other_readers(world):
    await world.start(ALICE)
    async with world.maker() as reader, reader.begin():
        # Another claim's identity check holds the link lock shared.
        await reader.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:ns, 0))"),
            {"ns": "github-link"},
        )
        held = await asyncio.wait_for(world.hold(ALICE), timeout=10)
    assert held.status_code == 200, held.text


async def test_only_the_hold_response_carries_the_rehearsal_receipt(world):
    await world.start(ALICE)
    body = world.body(ALICE)
    claim = (await world.client.post(CLAIMS, json=body)).json()
    assert claim["simulated_receipt"] is not None
    # An exact retry is the signer's own request, so it carries it too.
    again = (await world.client.post(CLAIMS, json=body)).json()
    assert again["simulated_receipt"] == claim["simulated_receipt"]
    read = (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()
    assert read["state"] == "held" and read["simulated_receipt"] is None
    # The public board names the claim, but its id alone pays nothing.
    guessed = hashlib.sha256(
        f"ditto-bounty-simulated-fee:v1:{claim['claim_id']}".encode()
    ).hexdigest()
    forged = await world.confirm(
        claim, {"block_hash": f"0x{guessed}", "block_number": 0, "extrinsic_index": 0}
    )
    assert forged.status_code == 402 and "rehearsal fee receipt" in forged.text
    assert (await world.counts())["bounty_fee_payments"] == 0
    paid = await world.confirm(claim)
    assert paid.status_code == 200 and paid.json()["state"] == "claimed"
    assert paid.json()["simulated_receipt"] is None


async def test_receipts_are_bound_to_their_claim_and_charged_once(world):
    await world.start(ALICE, BOB)
    alice = (await world.hold(ALICE)).json()
    bob = (await world.hold(BOB)).json()
    # Under the simulated verifier a receipt pays exactly one claim's fee.
    wrong = await world.confirm(alice, bob["simulated_receipt"])
    assert wrong.status_code == 402 and "rehearsal fee receipt" in wrong.text

    class OneReceipt:
        """A verifier that accepts one shared receipt for every claim."""

        async def verify(self, receipt, *, claim):
            if receipt.block_hash != "0x" + "11" * 32:
                raise BountyFeeRejected("unknown receipt")
            return VerifiedBountyFee(
                block_hash=receipt.block_hash,
                block_number=receipt.block_number,
                extrinsic_index=receipt.extrinsic_index,
                payer_coldkey=claim.payee_coldkey,
                amount_alpha_rao=claim.fee_alpha_rao,
                simulated=True,
            )

    world.app.state.bounty_fee_verifier = OneReceipt()
    shared = {"block_hash": "0x" + "11" * 32, "block_number": 7, "extrinsic_index": 2}
    assert (await world.confirm(alice, shared)).json()["state"] == "claimed"
    reused = await world.confirm(bob, shared)
    assert reused.status_code == 409 and "already paid another claim" in reused.text
    assert (await world.counts())["bounty_fee_payments"] == 1


async def test_a_second_payment_for_a_claimed_slot_is_refunded(world):
    await world.start(ALICE)
    claim = (await world.hold(ALICE)).json()
    assert (await world.confirm(claim)).json()["state"] == "claimed"

    class AnyReceipt:
        async def verify(self, receipt, *, claim):
            return VerifiedBountyFee(
                block_hash=receipt.block_hash,
                block_number=receipt.block_number,
                extrinsic_index=receipt.extrinsic_index,
                payer_coldkey=claim.payee_coldkey,
                amount_alpha_rao=claim.fee_alpha_rao,
                simulated=True,
            )

    world.app.state.bounty_fee_verifier = AnyReceipt()
    second = {"block_hash": "0x" + "22" * 32, "block_number": 9, "extrinsic_index": 0}
    duplicate = (await world.confirm(claim, second)).json()
    assert duplicate["state"] == "claimed"
    assert [r["reason"] for r in duplicate["refunds"]] == ["duplicate_payment"]
    assert duplicate["refunds"][0]["receipt"] == second


async def test_a_receipt_for_another_amount_or_payer_is_refused(world):
    await world.start(ALICE)
    claim = (await world.hold(ALICE)).json()

    class Short:
        async def verify(self, receipt, *, claim):
            return VerifiedBountyFee(
                block_hash=receipt.block_hash,
                block_number=0,
                extrinsic_index=0,
                payer_coldkey=claim.payee_coldkey,
                amount_alpha_rao=claim.fee_alpha_rao - 1,
                simulated=True,
            )

    world.app.state.bounty_fee_verifier = Short()
    refused = await world.confirm(claim)
    assert refused.status_code == 402 and "exact fee" in refused.text
    assert (await world.counts())["bounty_fee_payments"] == 0


async def test_unknown_claims_and_receipt_shapes(world):
    await world.start(ALICE)
    missing = await world.client.get(f"{CLAIMS}/{uuid4()}")
    assert missing.status_code == 404
    claim = (await world.hold(ALICE)).json()
    malformed = await world.client.post(
        f"{CLAIMS}/{claim['claim_id']}/confirm",
        json={
            "receipt": {"block_hash": "0x12", "block_number": 0, "extrinsic_index": 0}
        },
    )
    assert malformed.status_code == 422


async def test_pausing_stops_new_holds_but_never_a_payment(world):
    await world.start(ALICE, BOB)
    claim = (await world.hold(ALICE)).json()
    bounty_id = world.detail["summary"]["bounty_id"]
    route = f"/api/v1/admin/bounties/{bounty_id}/claims-paused"
    body = {"paused": True, "reason": "fixture pause for review"}
    wrong = await world.client.post(
        route, headers=_HEADERS, json={**body, "confirmation": "PAUSE"}
    )
    assert wrong.status_code == 409 and "PAUSE BOUNTY CLAIMS" in wrong.text
    phrase = f"PAUSE BOUNTY CLAIMS {REPO}#{ISSUE}"
    paused = await world.client.post(
        route, headers=_HEADERS, json={**body, "confirmation": phrase}
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["claims_paused"] is True
    twice = await world.client.post(
        route, headers=_HEADERS, json={**body, "confirmation": phrase}
    )
    assert twice.status_code == 409 and "already paused" in twice.text
    refused = await world.hold(BOB)
    assert refused.status_code == 409 and "paused" in refused.text
    # A hold placed before the pause still completes.
    assert (await world.confirm(claim)).json()["state"] == "claimed"
    detail = await world.refresh()
    assert detail["summary"]["claims_paused"] is True
    assert detail["events"][-2]["kind"] == "claims_paused"
    # The operator and reason stay private.
    assert _ACTOR not in str(detail) and "fixture pause" not in str(detail)
    resumed = await world.client.post(
        route,
        headers=_HEADERS,
        json={
            "paused": False,
            "reason": "fixture review done",
            "confirmation": f"RESUME BOUNTY CLAIMS {REPO}#{ISSUE}",
        },
    )
    assert resumed.status_code == 200 and resumed.json()["claims_paused"] is False
    assert (await world.hold(BOB)).status_code == 200
    async with world.maker() as session:
        controls = list(await session.scalars(select(BountyClaimControl)))
    assert [(c.paused, c.actor) for c in controls] == [(True, _ACTOR), (False, _ACTOR)]


async def test_pause_requires_the_admin_token_and_an_actor(world):
    await world.start(ALICE)
    bounty_id = world.detail["summary"]["bounty_id"]
    route = f"/api/v1/admin/bounties/{bounty_id}/claims-paused"
    body = {
        "paused": True,
        "reason": "fixture pause for review",
        "confirmation": f"PAUSE BOUNTY CLAIMS {REPO}#{ISSUE}",
    }
    assert (await world.client.post(route, json=body)).status_code == 401
    unnamed = await world.client.post(route, headers=_ADMIN_TOKEN, json=body)
    assert unnamed.status_code == 422
    unknown = await world.client.post(
        f"/api/v1/admin/bounties/{uuid4()}/claims-paused", headers=_HEADERS, json=body
    )
    assert unknown.status_code == 404
    assert (await world.counts())["bounty_claim_controls"] == 0


async def test_claim_history_is_append_only(world):
    await world.start(ALICE)
    claim = (await world.hold(ALICE)).json()
    assert (await world.confirm(claim)).status_code == 200
    history = "bounty history is append-only"
    for statement in (
        "UPDATE bounty_claims SET fee_alpha_rao = 1",
        "DELETE FROM bounty_claims",
        "UPDATE bounty_action_nonces SET action = 'claim'",
        "UPDATE bounty_fee_payments SET outcome = 'refund_owed'",
        "DELETE FROM bounty_fee_payments",
        "TRUNCATE bounty_obligations CASCADE",
        "TRUNCATE bounty_claim_controls",
    ):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=history):
                await session.execute(text(statement))
            await session.rollback()
    async with world.maker() as session:
        with pytest.raises(DBAPIError, match="only a bounty slot status can change"):
            await session.execute(text("UPDATE bounty_slots SET position = 7"))
        await session.rollback()
    async with world.maker() as session:
        # A claimed slot must carry its claim and deadline.
        with pytest.raises(DBAPIError, match="ck_bounty_slots_claim"):
            await session.execute(
                text("UPDATE bounty_slots SET deadline_at = NULL WHERE position = 1")
            )
        await session.rollback()


async def test_an_obligation_belongs_to_its_claims_bounty(world):
    await world.start(ALICE)
    claim = (await world.hold(ALICE)).json()
    assert (await world.confirm(claim)).status_code == 200
    other = await publish_bounty(world.client, issue=ISSUE + 1)
    assert other.status_code == 200, other.text
    # A refund of the claim's own fee, recorded under the bounty with `issue`.
    refund = text(
        "INSERT INTO bounty_obligations (obligation_id, bounty_id, claim_id, kind,"
        " reason, payee_coldkey, amount_alpha_rao, block_hash, extrinsic_index,"
        " simulated, created_at) SELECT gen_random_uuid(), b.bounty_id, p.claim_id,"
        " 'fee_refund', 'system_fault', p.payer_coldkey, p.amount_alpha_rao,"
        " p.block_hash, p.extrinsic_index, p.simulated, now()"
        " FROM bounty_fee_payments p JOIN bounties b ON b.issue_number = :issue"
    )
    async with world.maker() as session:
        with pytest.raises(DBAPIError, match="violates foreign key"):
            await session.execute(refund, {"issue": ISSUE + 1})
        await session.rollback()
    async with world.maker() as session, session.begin():
        await session.execute(refund, {"issue": ISSUE})


async def test_claim_routes_declare_their_errors(app):
    paths = app.openapi()["paths"]
    assert {"400", "403", "404", "409", "429", "503"} <= set(
        paths[CLAIMS]["post"]["responses"]
    )
    assert {"402", "404", "409", "503"} <= set(
        paths[f"{CLAIMS}/{{claim_id}}/confirm"]["post"]["responses"]
    )
    assert {"401", "404", "409"} <= set(
        paths["/api/v1/admin/bounties/{bounty_id}/claims-paused"]["post"]["responses"]
    )


async def test_simulated_verifier_refuses_live_funding():
    claim = BountyClaim(
        claim_id=uuid4(),
        funding_mode="live",
        payee_coldkey=ALICE.coldkey.ss58_address,
        fee_alpha_rao=1,
        signature="ab" * 64,
    )
    receipt = BountyFeeReceipt(
        block_hash=simulated_fee_block_hash(claim.claim_id, claim.signature),
        block_number=0,
        extrinsic_index=0,
    )
    with pytest.raises(BountyFeeRejected, match="live fee verifier"):
        await bounty_claims.SimulatedBountyFeeVerifier().verify(receipt, claim=claim)
