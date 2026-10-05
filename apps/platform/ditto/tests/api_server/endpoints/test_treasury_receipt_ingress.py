"""Real PostgreSQL ingress + canonical reader with synthetic public RPC transport."""

import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from bittensor_wallet import Keypair
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from ditto.api_models.treasury_settings import TreasurySettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.ledger_pin import ledger_digest
from ditto.api_server.treasury_ingress import digest
from ditto.chain.treasury_receipts import finalized_block, read_treasury_chain_proof
from ditto.db.models import (
    LedgerEpochSnapshot,
    TreasuryPublicEvent,
    TreasurySettingsRevision,
    TreasuryVerifiedReceipt,
)
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_CODE_HASH,
    FINNEY_GENESIS,
    HISTORICAL_COLLECTOR_CODE_HASH,
)
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import approval_message
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin

pytestmark = pytest.mark.asyncio
URL = "/api/v1/admin/treasury-receipts"
TOKEN = "synthetic-ingress-admin-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def h(block):
    return FINNEY_GENESIS if block == 0 else f"0x{block:064x}"


def event(module, name, attrs, index=0, phase="ApplyExtrinsic"):
    return {
        "module_id": module,
        "event_id": name,
        "extrinsic_idx": index,
        "phase": phase,
        "event": {"attributes": attrs},
    }


def fixture():
    raw = json.loads(
        (
            Path(__file__).resolve().parents[6]
            / "packages/ditto-screening-protocol/tests/fixtures"
            / "treasury_enforcing_pin_v2.json"
        ).read_text()
    )
    raw["policy"]["genesis_hash"] = FINNEY_GENESIS
    policy = TreasuryEmissionPolicy.model_validate(raw["policy"])
    raw["policy_digest"] = policy.digest
    raw["pinned_block_hash"] = h(101)
    raw["identity"].update(genesis_hash=FINNEY_GENESIS, finalized_block_hash=h(101))
    raw["approval"] = {
        "policy": policy.model_dump(mode="json"),
        "signature": "0x"
        + Keypair.create_from_uri("//Alice").sign(approval_message(policy)).hex(),
    }
    raw["fleet"][0]["approved_policy_digest"] = policy.digest
    pin = EnforcingTreasuryPin.model_validate(raw)
    settings = TreasurySettings(
        allocation_version=2,
        treasury_hotkey=policy.collector_hotkey,
        treasury_coldkey=policy.collector_coldkey,
        service_buckets=[
            {
                "bucket_id": "gamma",
                "purpose": "Gamma vendor credits",
                "allocation_bps": 1000,
                "holding_coldkey": policy.buckets[0].holding_coldkey,
                "service_account_ref": "PRIVATE-BILLING-ID",
                "payee_rules": [
                    {
                        "rule_id": "gamma_treasury",
                        "label": "Gamma vendor payment",
                        "recipient_coldkey": Keypair.create_from_uri(
                            "//Eve"
                        ).ss58_address,
                        "asset": "TAO",
                    },
                    {
                        "rule_id": "alpha_payee",
                        "label": "Exact alpha vendor",
                        "recipient_coldkey": Keypair.create_from_uri(
                            "//Eve"
                        ).ss58_address,
                        "recipient_hotkey": policy.collector_hotkey,
                        "asset": "SN118_ALPHA",
                    },
                ],
            }
        ],
    )
    return pin, settings


class RPC:
    def __init__(self, pin, settings):
        self.policy = pin.policy
        self.vendor = settings.service_buckets[0].payee_rules[0].recipient_coldkey
        self.hashes = {}
        self.height = 200
        self.code = AUDITED_COLLECTOR_CODE_HASH
        self.route = pin.policy.collector_hotkey
        self.owner = pin.policy.collector_coldkey
        self.transfer_events = [
            event("System", "ExtrinsicSuccess", {}, index=1),
            event("Proxy", "ProxyExecuted", {"result": {"Ok": []}}),
            event("System", "ExtrinsicSuccess", {}),
            event(
                "SubtensorModule",
                "StakeRemoved",
                [self.owner, self.policy.collector_hotkey, 999, 40, 118, 0],
            ),
            event(
                "SubtensorModule",
                "StakeAdded",
                [
                    self.policy.buckets[0].holding_coldkey,
                    self.policy.collector_hotkey,
                    999,
                    40,
                    118,
                    0,
                ],
            ),
        ]
        self.vendor_events = [
            event("System", "ExtrinsicSuccess", {}),
            event(
                "Balances",
                "Transfer",
                {
                    "from": self.policy.buckets[0].holding_coldkey,
                    "to": self.vendor,
                    "amount": 25,
                },
            ),
        ]
        self.source_events = [
            event(
                "SubtensorModule",
                "IncentiveAlphaEmittedToMiners",
                {"netuid": 118, "emissions": [100]},
                index=None,
                phase="Initialization",
            ),
            event(
                "SubtensorModule",
                "AutoStakeAdded",
                {
                    "netuid": 118,
                    "destination": self.policy.collector_hotkey,
                    "hotkey": self.policy.collector_hotkey,
                    "owner": self.owner,
                    "incentive": 40,
                },
                index=None,
                phase="Initialization",
            ),
        ]
        self.signer = None
        self.epoch = 9
        self.rebound_uid = None
        self.distribution_blocks = {120}
        self.proxy_vendor = False

    async def get_chain_finalised_head(self):
        return h(self.height)

    async def get_block_number(self, at):
        return int(at[2:], 16)

    async def get_block_hash(self, block):
        return self.hashes.get(block, h(block))

    async def rpc_request(self, method, params):
        assert isinstance(params, list) and params
        if method == "state_getStorageHash":
            return {"result": self.code}
        return {"result": {"block": {"extrinsics": ["0xab"]}}}

    async def get_block(self, *, block_hash):
        if int(block_hash[2:], 16) in self.distribution_blocks or self.proxy_vendor:
            return {
                "extrinsics": [
                    {
                        "address": self.signer or self.vendor,
                        "call": {
                            "call_module": "Proxy",
                            "call_function": "proxy",
                            "call_args": [
                                {
                                    "name": "real",
                                    "value": self.owner
                                    if int(block_hash[2:], 16)
                                    in self.distribution_blocks
                                    else self.policy.buckets[0].holding_coldkey,
                                }
                            ],
                        },
                    }
                ]
            }
        return {
            "extrinsics": [
                {
                    "address": self.signer or self.policy.buckets[0].holding_coldkey,
                    "call": {
                        "call_module": "Balances",
                        "call_function": "transfer_keep_alive",
                        "call_args": [],
                    },
                }
            ]
        }

    async def query(self, *, module, storage_function, params, block_hash):
        assert module in {"System", "SubtensorModule", "Timestamp"}
        assert isinstance(params, list)
        if storage_function == "Events":
            return (
                self.source_events
                if block_hash == h(110)
                else self.transfer_events
                if int(block_hash[2:], 16) in self.distribution_blocks
                else self.vendor_events
            )
        uid = (
            self.rebound_uid
            if self.rebound_uid is not None and int(block_hash[2:], 16) > 101
            else 0
        )
        return {
            "Owner": self.owner,
            "SubnetOwner": Keypair.create_from_uri("//Dave").ss58_address,
            "Uids": uid,
            "Keys": self.policy.collector_hotkey,
            "AutoStakeDestination": self.route,
            "SubnetEpochIndex": self.epoch,
            "Now": 1_600_000_000_000 + int(block_hash[2:], 16) * 1000,
        }[storage_function]


class Chain:
    def __init__(self, rpc):
        self.rpc = rpc

    async def get_treasury_receipt_proof(self, selector, policy, **destination):
        return await read_treasury_chain_proof(
            self.rpc, selector, policy, **destination
        )


async def install(app, session_maker, *, publish=True, fault=None):
    pin, settings = fixture()
    if fault == "signature":
        pin = pin.model_copy(
            update={
                "approval": pin.approval.model_copy(
                    update={"signature": "0x" + "00" * 64}
                )
            }
        )
    elif fault == "pin_identity":
        pin = pin.model_copy(
            update={"identity": pin.identity.model_copy(update={"uid": 7})}
        )
    settings = settings.model_copy(
        update={
            "service_buckets": [
                settings.service_buckets[0].model_copy(
                    update={"publish_payments": publish}
                )
            ]
        }
    )
    served = {"treasury_pin": pin.model_dump(mode="json")}
    async with session_maker() as session:
        session.add(
            TreasurySettingsRevision(
                revision=1,
                parent_revision=0,
                settings=settings.model_dump(mode="json"),
                checksum=digest(settings.model_dump(mode="json"))
                if fault != "checksum"
                else "0" * 64,
                actor="PRIVATE-HUMAN",
                reason="PRIVATE-AUDIT-REASON",
                created_at=datetime(
                    2030 if fault == "future_policy" else 2020, 1, 1, tzinfo=UTC
                ),
            )
        )
        session.add(
            LedgerEpochSnapshot(
                snapshot_id=uuid4(),
                netuid=118,
                epoch_index=9,
                last_epoch_block=100,
                pinned_block=101,
                pinned_block_hash=h(101),
                pinned_at=datetime(2020, 1, 1, tzinfo=UTC),
                bench_version=13,
                entries=[],
                context={"served": served},
                ledger_digest=ledger_digest([], served)
                if fault != "ledger"
                else "0" * 64,
            )
        )
        await session.commit()
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    app.state.session_maker = session_maker

    async def db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = db
    rpc = RPC(pin, settings)
    app.state.chain = Chain(rpc)
    return pin, settings, rpc


def selection(stage="service_distribution"):
    return {
        "stage": stage,
        "epoch_index": 9,
        "bucket_id": "gamma",
        "source_block": 110 if stage == "service_distribution" else None,
        "block": 120 if stage == "service_distribution" else 130,
        "block_hash": h(120 if stage == "service_distribution" else 130),
        "extrinsic_index": 0,
        "extrinsic_hash": "0x"
        + hashlib.blake2b(bytes.fromhex("ab"), digest_size=32).hexdigest(),
        "amount_atomic": 40 if stage == "service_distribution" else 25,
        "payee_rule_id": None if stage == "service_distribution" else "gamma_treasury",
        "reason": "Observe synthetic public receipt",
    }


async def test_real_ingress_replay_independent_vendor_and_history(
    app, client, session_maker
):
    pin, settings, _ = await install(app, session_maker)
    response = await client.post(URL, headers=HEADERS, json=selection())
    assert response.status_code == 200, response.text
    assert response.json()["published"] and not response.json()["replayed"]
    replay = await client.post(
        URL,
        headers=HEADERS,
        json={
            **selection(),
            "actor": "FORGED",
            "finalized": True,
            "credited_usd_nano": "FAKE",
        },
    )
    assert replay.status_code == 200 and replay.json()["replayed"]
    # A newer wallet/publication revision cannot rewrite this historical receipt.
    async with session_maker() as session:
        later = settings.model_copy(
            update={
                "service_buckets": [
                    settings.service_buckets[0].model_copy(
                        update={"publish_payments": False}
                    )
                ]
            }
        )
        session.add(
            TreasurySettingsRevision(
                revision=2,
                parent_revision=1,
                settings=later.model_dump(mode="json"),
                checksum=digest(later.model_dump(mode="json")),
                reason="New publication proposal",
                actor="new-operator",
            )
        )
        await session.commit()
    vendor = await client.post(URL, headers=HEADERS, json=selection("vendor_payment"))
    assert vendor.status_code == 200, vendor.text
    assert (
        vendor.json()["source_block"] is None
        and vendor.json()["provider_credit_status"] == "not_proven"
    )
    feed = (await client.get("/api/v1/public/treasury-activity")).json()["items"]
    assert [i["event_kind"] for i in feed] == ["vendor_payment", "service_distribution"]
    assert (
        feed[0]["source_alpha_rao"] == "0"
        and feed[0]["denominator"] == "not_attributed"
    )
    assert (
        feed[1]["deposit_amount_atomic"] == "40" and feed[1]["source_alpha_rao"] == "40"
    )
    assert feed[1]["event_index"] == 4 and feed[1]["policy_digest"] == pin.policy.digest
    assert "PRIVATE" not in json.dumps(feed)
    async with session_maker() as session:
        row = await session.get(TreasuryVerifiedReceipt, response.json()["receipt_id"])
        assert row.actor == "platform_admin_token"
        with pytest.raises(DBAPIError, match="append only"):
            await session.execute(
                update(TreasuryVerifiedReceipt).values(reason="rewritten")
            )
        await session.rollback()


async def test_publication_off_is_private_durable_not_dropped(
    app, client, session_maker
):
    await install(app, session_maker, publish=False)
    response = await client.post(URL, headers=HEADERS, json=selection())
    assert response.status_code == 200, response.text
    assert (
        response.json()["published"] is False
        and response.json()["public_event_id"] is None
    )
    assert (await client.get("/api/v1/public/treasury-activity")).json()["items"] == []
    assert len((await client.get(URL, headers=HEADERS)).json()["items"]) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "checksum",
        "signature",
        "pin_identity",
        "future_policy",
        "ledger",
        "nonfinal",
        "reorg",
        "runtime",
        "route",
        "identity",
        "rebound_uid",
        "inner_failed",
        "outer_failed",
        "amount",
        "duplicate",
        "epoch",
        "payee",
        "provider",
    ],
)
async def test_negative_canonical_proofs_never_write(app, client, session_maker, fault):
    _, _, rpc = await install(app, session_maker, fault=fault)
    payload = selection()
    if fault == "nonfinal":
        rpc.height = 119
    elif fault == "reorg":
        rpc.hashes[120] = h(999)
    elif fault == "runtime":
        rpc.code = "0x" + "00" * 32
    elif fault == "route":
        rpc.route = rpc.vendor
    elif fault == "identity":
        rpc.owner = rpc.vendor
    elif fault == "rebound_uid":
        rpc.rebound_uid = 7
    elif fault == "inner_failed":
        rpc.transfer_events[1]["event"]["attributes"] = {
            "result": {"Err": "NoPermission"}
        }
    elif fault == "outer_failed":
        rpc.transfer_events.append(event("System", "ExtrinsicFailed", {}))
    elif fault == "amount":
        payload["amount_atomic"] = 41
    elif fault == "duplicate":
        rpc.transfer_events.append(copy.deepcopy(rpc.transfer_events[-1]))
    elif fault == "epoch":
        rpc.epoch = 10
    elif fault == "payee":
        payload = selection("vendor_payment")
        rpc.vendor_events[-1]["event"]["attributes"]["to"] = rpc.owner
    elif fault == "provider":
        payload = selection("provider_credit")
    response = await client.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == 422, response.text
    async with session_maker() as session:
        assert list(await session.scalars(select(TreasuryVerifiedReceipt))) == []
        assert list(await session.scalars(select(TreasuryPublicEvent))) == []


async def test_conflicting_source_claim_and_auth_refuse(app, client, session_maker):
    await install(app, session_maker)
    assert (await client.post(URL, json=selection())).status_code == 401
    first = await client.post(URL, headers=HEADERS, json=selection())
    assert first.status_code == 200
    wrong = selection()
    wrong["bucket_id"] = "other_bucket"
    assert (await client.post(URL, headers=HEADERS, json=wrong)).status_code == 422


async def test_independent_tao_vendor_does_not_require_current_collector_uid(
    app, client, session_maker
):
    _, _, rpc = await install(app, session_maker)
    rpc.rebound_uid = 7
    result = await client.post(URL, headers=HEADERS, json=selection("vendor_payment"))
    assert result.status_code == 200, result.text
    assert result.json()["source_block"] is None


async def test_concurrent_replay_and_second_source_effect_are_fenced(
    app, client, session_maker
):
    _, _, rpc = await install(app, session_maker)
    responses = await asyncio.gather(
        *[client.post(URL, headers=HEADERS, json=selection()) for _ in range(3)]
    )
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert sum(not r.json()["replayed"] for r in responses) == 1
    rpc.distribution_blocks.add(121)
    other = {**selection(), "block": 121, "block_hash": h(121)}
    assert (await client.post(URL, headers=HEADERS, json=other)).status_code == 409
    async with session_maker() as db:
        assert len(list(await db.scalars(select(TreasuryVerifiedReceipt)))) == 1
        assert len(list(await db.scalars(select(TreasuryPublicEvent)))) == 1


async def test_linked_alpha_payments_cannot_overspend_distribution(
    app, client, session_maker
):
    _, _, rpc = await install(app, session_maker)
    distribution = await client.post(URL, headers=HEADERS, json=selection())
    assert distribution.status_code == 200, distribution.text
    rpc.proxy_vendor = True

    def effects(amount):
        return [
            event("System", "ExtrinsicSuccess", {}),
            event("Proxy", "ProxyExecuted", {"result": {"Ok": []}}),
            event(
                "SubtensorModule",
                "StakeRemoved",
                [
                    rpc.policy.buckets[0].holding_coldkey,
                    rpc.policy.collector_hotkey,
                    999,
                    amount,
                    118,
                    0,
                ],
            ),
            event(
                "SubtensorModule",
                "StakeAdded",
                [rpc.vendor, rpc.policy.collector_hotkey, 999, amount, 118, 0],
            ),
        ]

    rpc.vendor_events = effects(25)
    first = {
        **selection("vendor_payment"),
        "source_block": 110,
        "parent_receipt_id": distribution.json()["receipt_id"],
        "payee_rule_id": "alpha_payee",
    }
    result = await client.post(URL, headers=HEADERS, json=first)
    assert result.status_code == 200, result.text
    rpc.vendor_events = effects(20)
    second = {**first, "block": 131, "block_hash": h(131), "amount_atomic": 20}
    assert (await client.post(URL, headers=HEADERS, json=second)).status_code == 409
    async with session_maker() as db:
        assert len(list(await db.scalars(select(TreasuryVerifiedReceipt)))) == 2
    wrong = selection()
    wrong["amount_atomic"] = 41
    # Same canonical effect coordinates with changed selector is a conflict,
    # and wrong chain effect can refuse even before reaching the replay fence.
    assert (await client.post(URL, headers=HEADERS, json=wrong)).status_code in {
        409,
        422,
    }


@pytest.mark.parametrize(
    "parent,post,allowed",
    [
        (HISTORICAL_COLLECTOR_CODE_HASH, HISTORICAL_COLLECTOR_CODE_HASH, True),
        (AUDITED_COLLECTOR_CODE_HASH, AUDITED_COLLECTOR_CODE_HASH, True),
        (HISTORICAL_COLLECTOR_CODE_HASH, AUDITED_COLLECTOR_CODE_HASH, True),
        (AUDITED_COLLECTOR_CODE_HASH, HISTORICAL_COLLECTOR_CODE_HASH, False),
        ("0x" + "aa" * 32, AUDITED_COLLECTOR_CODE_HASH, False),
        (AUDITED_COLLECTOR_CODE_HASH, "0x" + "aa" * 32, False),
    ],
)
async def test_exact_historical_execution_runtime_and_forward_upgrade(
    parent, post, allowed
):
    class HistoricalRPC:
        async def get_chain_finalised_head(self):
            return h(200)

        async def get_block_number(self, _):
            return 200

        async def get_block_hash(self, n):
            return FINNEY_GENESIS if n == 0 else h(n)

        async def rpc_request(self, _method, params):
            return {"result": parent if params[-1] == h(119) else post}

    if allowed:
        at, previous, code = await finalized_block(HistoricalRPC(), 120, FINNEY_GENESIS)
        assert (at, previous, code) == (h(120), h(119), parent)
    else:
        with pytest.raises(ValueError, match="runtime"):
            await finalized_block(HistoricalRPC(), 120, FINNEY_GENESIS)
