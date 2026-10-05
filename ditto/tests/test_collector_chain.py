"""Audited decode shapes and adversarial receipt/attribution controls."""

import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ditto.tests.test_collector_automation import policy
from ditto.treasury.collector import Observation, SignedOperation
from ditto.treasury.collector_chain import (
    AUDITED_CODE_HASH,
    FINNEY_GENESIS,
    NoCredentialRedirect,
    PublicCollectorChain,
)


def identity_adapter(*, role="registration", owner="cold", uid=None, raw_owner=None):
    chain = PublicCollectorChain.__new__(PublicCollectorChain)
    chain.role = role
    values = {"Owner": owner, "SubnetOwner": "subnet-owner", "Uids": uid, "Keys": "hot"}
    calls = []
    chain.query = lambda _module, name, _params, _hash: values[name]
    chain.substrate = SimpleNamespace(
        create_storage_key=lambda *_args, **_kwargs: SimpleNamespace(
            to_hex=lambda: "0xowner"
        ),
        rpc_request=lambda method, params: (
            calls.append((method, params)) or {"result": raw_owner}
        ),
    )
    return (
        chain,
        SimpleNamespace(collector_coldkey="cold", collector_hotkey="hot"),
        values,
        calls,
    )


def test_first_registration_accepts_proven_absent_owner_only():
    c, p, values, calls = identity_adapter(owner="default-zero")
    assert c.identity(p, "finalized", allow_unowned=True) is None
    assert calls == [("state_getStorageAt", ["0xowner", "finalized"])]
    values["Owner"] = "cold"
    values["Uids"] = 14
    assert c.identity(p, "after-registration") == 14


@pytest.mark.parametrize("role", ["registration", "transfer"])
def test_unowned_collector_is_not_a_default_identity(role):
    c, p, _, _ = identity_adapter(role=role, owner="default-zero")
    with pytest.raises(ValueError, match="ownership"):
        c.identity(p, "finalized")


@pytest.mark.parametrize(
    "role,uid,raw_owner,response",
    [
        ("transfer", None, None, None),
        ("registration", 14, None, None),
        ("registration", None, "0x00", None),
        ("registration", None, None, {"error": "unavailable"}),
        ("registration", None, None, {}),
    ],
)
def test_unowned_bootstrap_refuses_existing_binding_and_uncertainty(
    role, uid, raw_owner, response
):
    c, p, _, _ = identity_adapter(
        role=role, owner="other-owner", uid=uid, raw_owner=raw_owner
    )
    if response is not None:
        c.substrate.rpc_request = lambda *_: response
    with pytest.raises(ValueError, match="ownership"):
        c.identity(p, "finalized", allow_unowned=True)


def test_unowned_bootstrap_cannot_use_subnet_owner_coldkey():
    c, p, values, _ = identity_adapter(owner="default-zero")
    values["SubnetOwner"] = p.collector_coldkey
    with pytest.raises(ValueError, match="subnet-owner"):
        c.identity(p, "finalized", allow_unowned=True)


def test_first_registration_observation_reaches_bounded_register_path():
    p = policy()
    c, _, _, _ = identity_adapter(owner="default-zero")
    c.guard_runtime = lambda *_: None
    c.assert_no_sponsor = lambda *_: None
    c.alpha = lambda *_: 0
    c.substrate.get_chain_finalised_head = lambda: "finalized"
    c.substrate.get_block_number = lambda _: 100
    values = {
        "Owner": "default-zero",
        "SubnetOwner": "subnet-owner",
        "Uids": None,
        "Burn": 1,
        "Account": {"data": {"free": 100}},
        "Proxies": (
            [
                {
                    "delegate": p.registration_delegate,
                    "proxy_type": "Registration",
                    "delay": 0,
                },
                {"delegate": p.transfer_delegate, "proxy_type": "Transfer", "delay": 0},
            ],
            0,
        ),
    }
    c.query = lambda _module, name, _params, _hash: values[name]
    observed = c.observe(p, "registration")
    assert observed.uid is None
    assert observed.burn_rao == 1
    assert observed.collector_free_rao == 100


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
    chain.identity = lambda _, h, **_kwargs: None if h == "b100" else 14
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


def test_first_registration_receipt_proves_new_owner_and_finalized_uid():
    p, c, op, observed, events = receipt_fixture()
    c.role = "registration"
    c.identity = PublicCollectorChain.identity.__get__(c)
    c.query = lambda _module, name, _params, block_hash: {
        "Owner": p.collector_coldkey if block_hash == "b101" else "default-zero",
        "SubnetOwner": "subnet-owner",
        "Uids": 14 if block_hash == "b101" else None,
        "Keys": p.collector_hotkey,
    }[name]
    original_rpc = c.substrate.rpc_request
    c.substrate.rpc_request = lambda method, params: (
        {"result": None}
        if method == "state_getStorageAt"
        else original_rpc(method, params)
    )
    c.substrate.create_storage_key = lambda *_args, **_kwargs: SimpleNamespace(
        to_hex=lambda: "0xowner"
    )
    op["role"] = "registration"
    events[2]["event"]["attributes"]["who"] = p.registration_delegate
    events[-2:] = [
        event("SubtensorModule", "NeuronRegistered", [118, 14, p.collector_hotkey])
    ]
    assert c.reconcile(p, op, observed).uid == 14
    c.substrate.rpc_request = lambda method, params: (
        {"result": "0xexisting-owner"}
        if method == "state_getStorageAt"
        else original_rpc(method, params)
    )
    with pytest.raises(ValueError, match="ownership"):
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


def inactive_income_fixture():
    p, c, events = income_fixture()
    c.identity = PublicCollectorChain.identity.__get__(c)
    c.query = lambda _module, name, _params, _hash: {
        "Uids": None,
        "Owner": "default-zero",
        "SubnetOwner": "other-subnet-owner",
        "Keys": p.collector_hotkey,
    }[name]
    c.substrate.create_storage_key = lambda *_args, **_kwargs: SimpleNamespace(
        to_hex=lambda: "0xowner"
    )
    c.substrate.rpc_request = lambda *_: {"result": None}
    events.pop()  # Other miners' tempo, no credit to this inactive collector.
    return p, c, events


def test_historical_unregistered_tempo_has_no_earnings_and_no_spend_authority():
    p, c, _ = inactive_income_fixture()
    assert c.earnings(p, 101) is None
    with pytest.raises(ValueError, match="ownership"):
        c.identity(p, "b101")  # Current transfer identity remains strict.
    with pytest.raises(ValueError, match="ownership"):
        c.identity(p, "b101", allow_unowned=True)


@pytest.mark.parametrize("response", [{}, {"error": "unavailable"}, {"result": "0xab"}])
def test_historical_absence_requires_explicit_raw_owner_absence(response):
    p, c, _ = inactive_income_fixture()
    c.substrate.rpc_request = lambda *_: response
    with pytest.raises(ValueError, match="ownership"):
        c.earnings(p, 101)


def test_historical_absence_with_collector_credit_halts():
    p, c, events = inactive_income_fixture()
    _, _, credited = income_fixture()
    events.append(credited[1])
    with pytest.raises(ValueError, match="exceeds gross"):
        c.earnings(p, 101)


def test_first_registration_tempo_remains_ambiguous():
    p, c, _ = inactive_income_fixture()
    absent_query = c.query
    c.query = lambda module, name, params, at: (
        {"Uids": 14, "Owner": p.collector_coldkey, "Keys": p.collector_hotkey}[name]
        if at == "b101" and name in {"Uids", "Owner", "Keys"}
        else absent_query(module, name, params, at)
    )
    with pytest.raises(ValueError, match="identity transition"):
        c.earnings(p, 101)


def test_signed_start_before_registration_advances_only_empty_history(tmp_path):
    from ditto.treasury.collector import CollectorJournal, tick

    p, c, events = inactive_income_fixture()
    events.clear()
    absent_query = c.query
    c.query = lambda module, name, params, at: (
        {"Uids": 14, "Owner": p.collector_coldkey, "Keys": p.collector_hotkey}[name]
        if int(at[1:]) >= 12 and name in {"Uids", "Owner", "Keys"}
        else absent_query(module, name, params, at)
    )
    c.observe = lambda *_: Observation(20, "b20", 14, 1, 100, 100, 0)
    c.prepare = lambda *_: pytest.fail("Empty history must never acquire a signer")
    journal = CollectorJournal(tmp_path / "transfer.db", p, "transfer", initialize=True)
    try:
        assert tick(journal, p, c, "transfer") == "observing"
        assert journal.db.execute("SELECT block FROM cursor").fetchone()[0] == 20
        assert journal.db.execute("SELECT COUNT(*) FROM earnings").fetchone()[0] == 0
        assert journal.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    finally:
        journal.close()


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


@pytest.mark.parametrize("locked", [None, {"locked": 0}])
def test_new_zero_position_does_not_require_omitted_availability(locked):
    p = policy()

    def call(_api, method, *_args, **_kwargs):
        if method == "get_stake_info_for_hotkey_coldkey_netuid":
            return {
                "hotkey": p.collector_hotkey,
                "coldkey": p.collector_coldkey,
                "netuid": 118,
                "stake": 0,
            }
        assert method == "get_stake_availability_for_coldkeys"
        return {p.collector_coldkey: {}}

    c = PublicCollectorChain.__new__(PublicCollectorChain)
    c.substrate = SimpleNamespace(runtime_call=call)
    c.query = lambda *_: locked
    assert c.alpha(p, p.collector_coldkey, "finalized") == 0


def test_zero_position_cannot_hide_positive_collateral_lock():
    p = policy()
    c = PublicCollectorChain.__new__(PublicCollectorChain)
    c.substrate = SimpleNamespace(
        runtime_call=lambda *_args, **_kwargs: {
            "hotkey": p.collector_hotkey,
            "coldkey": p.collector_coldkey,
            "netuid": 118,
            "stake": 0,
        }
    )
    c.query = lambda *_: {"locked": 1}
    with pytest.raises(ValueError, match="collateral"):
        c.alpha(p, p.collector_coldkey, "finalized")


@pytest.mark.parametrize(
    "policy_hash,live_hash",
    [
        (
            "0x5675b684d69a07f6f224c2ba9cabef719804911fba40fbe1a2295198c9cb7c47",
            "0x5675b684d69a07f6f224c2ba9cabef719804911fba40fbe1a2295198c9cb7c47",
        ),
        ("0x" + "a" * 64, "0x" + "a" * 64),
        (AUDITED_CODE_HASH, "0x" + "a" * 64),
    ],
)
def test_old_or_unaudited_runtime_remains_refused(policy_hash, live_hash):
    c = PublicCollectorChain.__new__(PublicCollectorChain)
    c.substrate = SimpleNamespace(
        get_block_hash=lambda _: FINNEY_GENESIS,
        rpc_request=lambda *_: {"result": live_hash},
        runtime_call=lambda *_args, **_kwargs: pytest.fail("must stop before APIs"),
    )
    with pytest.raises(ValueError, match="runtime"):
        c.guard_runtime(policy(runtime_code_hash=policy_hash), "finalized")


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


def registration_preparation(info):
    p = policy()
    observed = Observation(100, "finalized", None, 50, 2000, 200, 0)
    signed = []
    key = SimpleNamespace(ss58_address=p.registration_delegate)
    c = PublicCollectorChain.__new__(PublicCollectorChain)
    c.role = "registration"
    c.guard_runtime = lambda *_: None
    c.observe = lambda *_: observed
    c.key = lambda *_: key
    c.substrate = SimpleNamespace(
        get_chain_head=lambda: "head",
        compose_call=lambda *_args, **_kwargs: "proxy-call",
        get_account_nonce=lambda _: 3,
        get_payment_info=lambda *_args, **_kwargs: info,
        create_signed_extrinsic=lambda *args, **kwargs: (
            signed.append((args, kwargs))
            or SimpleNamespace(data=SimpleNamespace(to_hex=lambda: "0xab"))
        ),
    )
    call = {
        "module": "SubtensorModule",
        "function": "register_limit",
        "params": {"netuid": 118, "hotkey": p.collector_hotkey, "limit_price": 100},
    }
    return c, p, call, observed, signed


def test_registration_preparation_accepts_pinned_sdk_runtime_fee_shape():
    c, p, call, observed, signed = registration_preparation(
        {"partial_fee": 10, "class": "Normal", "weight": {"ref_time": 1}}
    )
    result = c.prepare(p, "registration", call, observed)
    assert result.fee_rao == p.max_fee_rao == 10
    assert result.encoded == "0xab"
    assert len(signed) == 1
    assert signed[0][1] == {"era": {"period": 64, "current": 100}, "nonce": 3, "tip": 0}


@pytest.mark.parametrize(
    "info",
    [
        None,
        {},
        {"partialFee": 1},
        {"partial_fee": True},
        {"partial_fee": "1"},
        {"partial_fee": -1},
        {"partial_fee": 2**64},
        {"partial_fee": 11},
    ],
)
def test_registration_preparation_refuses_missing_invalid_or_over_cap_fee(info):
    c, p, call, observed, signed = registration_preparation(info)
    with pytest.raises(ValueError):
        c.prepare(p, "registration", call, observed)
    assert not signed


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


def test_credential_redirect_is_refused_without_forwarding_authorization():
    with pytest.raises(RuntimeError, match="redirect refused"):
        NoCredentialRedirect().redirect_request(
            None, None, 302, None, None, "https://attacker.invalid"
        )


@pytest.mark.parametrize("unit", [None, [], ()])
def test_proxy_success_scale_unit_is_decoder_independent(unit):
    p, c, op, observed, events = receipt_fixture()
    events[0]["event"]["attributes"]["result"] = {"Ok": unit}
    assert c.reconcile(p, op, observed).status == "finalized"
    op["role"] = "registration"
    c.role = "registration"
    events[2]["event"]["attributes"]["who"] = p.registration_delegate
    events[-2:] = [
        event("SubtensorModule", "NeuronRegistered", [118, 14, p.collector_hotkey])
    ]
    assert c.reconcile(p, op, observed).uid == 14


@pytest.mark.parametrize(
    "result",
    [
        {"Ok": [1]},
        {"Ok": (1,)},
        {"Ok": {}},
        {"Ok": ""},
        {"Ok": 0},
        {"Ok": False},
        {"Ok": b""},
        {"Ok": (), "Err": "NoPermission"},
        {},
    ],
)
def test_proxy_nonunit_result_cannot_release_durable_claim(result):
    p, c, op, observed, events = receipt_fixture()
    events[0]["event"]["attributes"]["result"] = result
    with pytest.raises(ValueError):
        c.reconcile(p, op, observed)


@pytest.mark.parametrize("sequence", [list, tuple])
def test_registration_event_sequence_is_decoder_independent(sequence):
    p, c, op, observed, events = receipt_fixture()
    c.role = op["role"] = "registration"
    events[0]["event"]["attributes"] = {"result": {"Ok": ()}}
    events[2]["event"]["attributes"]["who"] = p.registration_delegate
    events[-2:] = [
        event(
            "SubtensorModule",
            "NeuronRegistered",
            sequence([118, 14, p.collector_hotkey]),
        )
    ]
    assert c.reconcile(p, op, observed).uid == 14


@pytest.mark.parametrize(
    "attrs",
    [
        (118, True, "hotkey"),
        (True, 14, "hotkey"),
        (118, "14", "hotkey"),
        (118, 15, "hotkey"),
        (119, 14, "hotkey"),
        (118, 14, "wrong"),
        (118, 14),
        (118, 14, "hotkey", 0),
        {"netuid": 118, "uid": 14, "hotkey": "hotkey"},
    ],
)
def test_registration_tuple_does_not_relax_exact_typed_effect(attrs):
    p, c, op, observed, events = receipt_fixture()
    c.role = op["role"] = "registration"
    events[2]["event"]["attributes"]["who"] = p.registration_delegate
    if isinstance(attrs, (list, tuple)):
        attrs = tuple(
            p.collector_hotkey if value == "hotkey" else value for value in attrs
        )
    events[-2:] = [event("SubtensorModule", "NeuronRegistered", attrs)]
    with pytest.raises(ValueError, match="registration effect"):
        c.reconcile(p, op, observed)


def test_registration_boolean_uid_cannot_equal_integer_one():
    p, c, op, observed, events = receipt_fixture()
    c.role = op["role"] = "registration"
    c.identity = lambda _p, at, **_kwargs: None if at == "b100" else 1
    events[2]["event"]["attributes"]["who"] = p.registration_delegate
    events[-2:] = [
        event("SubtensorModule", "NeuronRegistered", (118, True, p.collector_hotkey))
    ]
    with pytest.raises(ValueError, match="registration effect"):
        c.reconcile(p, op, observed)
