"""Live v470 decode shapes and adversarial receipt/attribution controls."""

import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ditto.tests.test_collector_automation import policy
from ditto.treasury.collector import Observation, SignedOperation
from ditto.treasury.collector_chain import PublicCollectorChain


def event(module, name, attrs, index=0, phase="ApplyExtrinsic"):
    return {
        "module_id": module,
        "event_id": name,
        "extrinsic_idx": index,
        "phase": phase,
        "event": {"attributes": attrs},
    }


def adapter(substrate):
    chain = PublicCollectorChain.__new__(PublicCollectorChain)
    chain.substrate = substrate
    chain.role = "transfer"
    chain.guard_runtime = lambda *_: None
    chain.identity = lambda _, h: None if h == "b100" else 14
    chain.alpha = lambda *_: 100
    return chain


def receipt_fixture():
    encoded = "0xab"
    digest = "0x" + hashlib.blake2b(bytes.fromhex("ab"), digest_size=32).hexdigest()
    signed = SignedOperation(encoded, digest, 100, 164, 5)
    p = policy()
    events = [
        event("Proxy", "ProxyExecuted", {"result": {"Ok": []}}),
        event("System", "ExtrinsicSuccess", {}),
        event(
            "TransactionPayment",
            "TransactionFeePaid",
            {"who": p.transfer_delegate, "actual_fee": 5, "tip": 0},
        ),
        event(
            "SubtensorModule",
            "StakeRemoved",
            [p.collector_coldkey, p.collector_hotkey, 999, 40, 118, 0],
        ),
        event(
            "SubtensorModule", "StakeAdded", ["gm", p.collector_hotkey, 999, 40, 118, 0]
        ),
    ]
    s = SimpleNamespace(
        get_block_hash=lambda n: f"b{n}",
        get_events=lambda _: events,
        rpc_request=lambda *_: {"result": {"block": {"extrinsics": [encoded]}}},
    )
    op = {
        "signed_json": json.dumps(asdict(signed)),
        "reconciled_through": 0,
        "role": "transfer",
        "amount": 40,
        "destination": "gm",
    }
    observed = Observation(101, "b101", 14, 1, 100, 100, 100)
    return p, adapter(s), op, observed, events


def test_real_proxy_unit_and_alpha_event_shapes_finalize():
    p, c, op, observed, _ = receipt_fixture()
    assert c.reconcile(p, op, observed).status == "finalized"


def test_outer_success_inner_err_is_failed_not_paid():
    p, c, op, observed, events = receipt_fixture()
    events[0]["event"]["attributes"]["result"] = {"Err": {"Module": "NoPermission"}}
    assert c.reconcile(p, op, observed).status == "failed"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_proxy",
        "unknown_proxy",
        "wrong_alpha",
        "wrong_destination",
        "missing_fee",
        "excess_fee",
        "wrong_payer",
        "missing_outer",
    ],
)
def test_unknown_or_conflicting_effect_never_finalizes(mutation):
    p, c, op, observed, events = receipt_fixture()
    if mutation == "missing_proxy":
        events.pop(0)
    elif mutation == "unknown_proxy":
        events[0]["event"]["attributes"]["result"] = {"Ok": "unexpected"}
    elif mutation == "wrong_alpha":
        events[-1]["event"]["attributes"][3] = 41
    elif mutation == "wrong_destination":
        events[-1]["event"]["attributes"][0] = "attacker"
    elif mutation == "missing_fee":
        events.pop(2)
    elif mutation == "excess_fee":
        events[2]["event"]["attributes"]["actual_fee"] = 11
    elif mutation == "wrong_payer":
        events[2]["event"]["attributes"]["who"] = p.collector_coldkey
    elif mutation == "missing_outer":
        events.pop(1)
    with pytest.raises(ValueError):
        c.reconcile(p, op, observed)


def test_effects_from_other_extrinsic_cannot_prove_transfer():
    p, c, op, observed, events = receipt_fixture()
    events[-1]["extrinsic_idx"] = 1
    with pytest.raises(ValueError, match="effect"):
        c.reconcile(p, op, observed)


def test_registration_requires_exact_finalized_uid_event():
    p, c, op, observed, events = receipt_fixture()
    op["role"] = "registration"
    events[2]["event"]["attributes"]["who"] = p.registration_delegate
    events[-2:] = [
        event("SubtensorModule", "NeuronRegistered", [118, 14, p.collector_hotkey])
    ]
    assert c.reconcile(p, op, observed).uid == 14
    events[-1]["event"]["attributes"][2] = "wrong-hotkey"
    with pytest.raises(ValueError, match="registration effect"):
        c.reconcile(p, op, observed)


def test_expiry_requires_all_finalized_mortal_blocks_scanned():
    p, c, op, observed, _ = receipt_fixture()
    c.substrate.rpc_request = lambda *_: {"result": {"block": {"extrinsics": []}}}
    observed = Observation(200, "b200", 14, 1, 100, 100, 100)
    first = c.reconcile(p, op, observed)
    assert first.status == "pending" and first.scanned_through == 132
    op["reconciled_through"] = 132
    assert c.reconcile(p, op, observed).status == "expired"


def income_fixture():
    p = policy()
    gross = event(
        "SubtensorModule",
        "IncentiveAlphaEmittedToMiners",
        {"netuid": 118, "emissions": [0] * 14 + [100]},
        None,
        "Initialization",
    )
    liquid = event(
        "SubtensorModule",
        "AutoStakeAdded",
        {
            "netuid": 118,
            "destination": p.collector_hotkey,
            "hotkey": p.collector_hotkey,
            "owner": p.collector_coldkey,
            "incentive": 20,
        },
        None,
        "Initialization",
    )
    events = [gross, liquid]
    s = SimpleNamespace(
        get_chain_finalised_head=lambda: "b200",
        get_block_number=lambda _: 200,
        get_block_hash=lambda n: f"b{n}",
        get_events=lambda _: events,
    )
    c = adapter(s)
    c.identity = lambda *_: 14
    c.query = lambda *_: p.collector_hotkey
    return p, c, events


def test_liquid_credit_only_never_gross_or_prior_principal():
    p, c, _ = income_fixture()
    assert c.earnings(p, 101).amount_rao == 20  # gross100, capture80


def test_fully_captured_gross_creates_no_distributable_income():
    p, c, events = income_fixture()
    events.pop()
    assert c.earnings(p, 101) is None


@pytest.mark.parametrize(
    "mutation",
    ["redirect", "exceeds_gross", "different_owner", "different_phase", "duplicate"],
)
def test_redirect_or_unproven_liquid_credit_cannot_spend_principal(mutation):
    p, c, events = income_fixture()
    attrs = events[1]["event"]["attributes"]
    if mutation == "redirect":
        c.query = lambda *_: "different-hotkey"
    elif mutation == "exceeds_gross":
        attrs["incentive"] = 101
    elif mutation == "different_owner":
        attrs["owner"] = "different-owner"
        assert c.earnings(p, 101) is None
        return
    elif mutation == "different_phase":
        events[1]["phase"] = "ApplyExtrinsic"
    elif mutation == "duplicate":
        events.append(events[1])
    with pytest.raises(ValueError):
        c.earnings(p, 101)


def test_real_collateral_and_aggregate_lock_bound_available_amount():
    p = policy()
    s = SimpleNamespace(
        runtime_call=lambda _api, method, *_args, **_kwargs: (
            {
                "hotkey": p.collector_hotkey,
                "coldkey": p.collector_coldkey,
                "netuid": 118,
                "stake": 100,
                "locked": 0,
            }
            if method == "get_stake_info_for_hotkey_coldkey_netuid"
            else {
                p.collector_coldkey: {
                    118: {"total": 100, "locked": 10, "available": 20}
                }
            }
        )
    )
    c = PublicCollectorChain.__new__(PublicCollectorChain)
    c.substrate = s
    c.query = lambda *_: {"locked": 70}
    assert c.alpha(p, p.collector_coldkey, "b101") == 20


@pytest.mark.parametrize(
    "function", ["burned_register", "transfer_all", "batch", "proxy", "set_auto_stake"]
)
def test_no_generic_or_unbounded_call_reaches_key(function):
    p = policy()
    c = adapter(object())
    c.key = lambda *_: pytest.fail("secret must not be accessed")
    with pytest.raises(ValueError, match="arbitrary"):
        c.prepare(
            p,
            "transfer",
            {"module": "SubtensorModule", "function": function, "params": {}},
            None,
        )


def test_phase_mismatch_cannot_claim_an_extrinsic_effect():
    p, c, op, observed, events = receipt_fixture()
    events[-1]["phase"] = "Initialization"
    with pytest.raises(ValueError, match="phase"):
        c.reconcile(p, op, observed)


@pytest.mark.parametrize(
    "raw", [{"result": "0x"}, {"result": "0x01"}, {}, {"error": "unavailable"}]
)
def test_raw_fee_sponsor_presence_or_unknown_never_collapses_to_absence(raw):
    p = policy()
    s = SimpleNamespace(
        create_storage_key=lambda *_args, **_kwargs: SimpleNamespace(
            to_hex=lambda: "0xkey"
        ),
        rpc_request=lambda *_: raw,
    )
    c = adapter(s)
    with pytest.raises(ValueError, match="sponsorship"):
        c.assert_no_sponsor(p, p.transfer_delegate, "b101")


def test_only_raw_absent_sponsorship_is_accepted():
    p = policy()
    s = SimpleNamespace(
        create_storage_key=lambda *_args, **_kwargs: SimpleNamespace(
            to_hex=lambda: "0xkey"
        ),
        rpc_request=lambda *_: {"result": None},
    )
    adapter(s).assert_no_sponsor(p, p.transfer_delegate, "b101")
