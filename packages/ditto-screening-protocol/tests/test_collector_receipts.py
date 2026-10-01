"""Effect decoding is separate from fixed-block finality and policy authority."""

import copy

import pytest

from ditto_screening_protocol.collector_receipts import (
    collector_gross_incentive,
    collector_transfer_effect,
    liquid_collector_credit,
)


def test_gross_credit_is_initialization_uid_specific_and_not_liquid():
    gross = event(
        "SubtensorModule",
        "IncentiveAlphaEmittedToMiners",
        {"netuid": 118, "emissions": [20, 100]},
        index=None,
        phase="Initialization",
    )
    assert collector_gross_incentive([gross], 1) == 100
    assert collector_gross_incentive([], 1) is None
    with pytest.raises(ValueError):
        collector_gross_incentive([gross, gross], 1)
    with pytest.raises(ValueError):
        collector_gross_incentive([gross], 2)
    with pytest.raises(ValueError):
        collector_gross_incentive([gross], True)
    gross["phase"] = "ApplyExtrinsic"
    with pytest.raises(ValueError):
        collector_gross_incentive([gross], 1)


def event(module, name, attrs, *, index=3, phase="ApplyExtrinsic"):
    return {
        "module_id": module,
        "event_id": name,
        "extrinsic_idx": index,
        "phase": phase,
        "event": {"attributes": attrs},
    }


def transfer_events():
    return [
        event("System", "ExtrinsicSuccess", {}, index=1),
        event("Proxy", "ProxyExecuted", {"result": {"Ok": []}}),
        event("System", "ExtrinsicSuccess", {}),
        event(
            "SubtensorModule", "StakeRemoved", ["collector", "hotkey", 999, 40, 118, 0]
        ),
        event("SubtensorModule", "StakeAdded", ["holding", "hotkey", 999, 40, 118, 0]),
    ]


def decode(events):
    return collector_transfer_effect(
        events,
        extrinsic_index=3,
        collector_coldkey="collector",
        collector_hotkey="hotkey",
        recipient_coldkey="holding",
        amount_rao=40,
    )


def test_exact_transfer_keeps_global_indexes_and_amount():
    result = decode(transfer_events())
    assert (
        result.amount_rao,
        result.removed_event_index,
        result.added_event_index,
    ) == (40, 3, 4)


@pytest.mark.parametrize(
    "mutation",
    [
        "inner_failed",
        "outer_failed",
        "duplicate",
        "recipient",
        "netuid",
        "amount",
        "hotkey",
        "phase",
        "bool_index",
        "bool_amount",
        "envelope",
    ],
)
def test_transfer_unproved_effect_refuses(mutation):
    events = transfer_events()
    if mutation == "inner_failed":
        events[1]["event"]["attributes"] = {"result": {"Err": "NoPermission"}}
    elif mutation == "outer_failed":
        events.append(event("System", "ExtrinsicFailed", {}))
    elif mutation == "duplicate":
        events.append(copy.deepcopy(events[4]))
    elif mutation == "phase":
        events[4]["phase"] = "Initialization"
    elif mutation == "bool_index":
        events[4]["extrinsic_idx"] = True
    elif mutation == "envelope":
        events[4]["event"] = None
    else:
        position, value = {
            "recipient": (0, "other"),
            "hotkey": (1, "other"),
            "amount": (3, 41),
            "bool_amount": (3, True),
            "netuid": (4, 119),
        }[mutation]
        events[4]["event"]["attributes"][position] = value
    with pytest.raises(ValueError):
        decode(events)


def credit_event():
    return event(
        "SubtensorModule",
        "AutoStakeAdded",
        {
            "netuid": 118,
            "hotkey": "hotkey",
            "owner": "collector",
            "destination": "hotkey",
            "incentive": 20,
        },
        index=None,
        phase="Initialization",
    )


def credit(events):
    return liquid_collector_credit(
        events,
        collector_hotkey="hotkey",
        collector_coldkey="collector",
        gross_incentive_rao=100,
    )


def test_liquid_not_gross_and_no_credit_is_not_violation():
    assert credit([]) is None
    result = credit([credit_event()])
    assert result.amount_rao == 20 and result.event_index == 0
    assert len(result.event_digest) == 64
    value = credit_event()
    value["event"]["attributes"]["incentive"] = 0
    assert credit([value]) is None


@pytest.mark.parametrize(
    "mutation", ["duplicate", "exceeds", "redirect", "phase", "bool_amount"]
)
def test_liquid_unproved_credit_refuses(mutation):
    value = credit_event()
    events = [value]
    if mutation == "duplicate":
        events.append(copy.deepcopy(value))
    elif mutation == "phase":
        value["phase"] = "ApplyExtrinsic"
    else:
        key, replacement = {
            "exceeds": ("incentive", 101),
            "redirect": ("destination", "other"),
            "bool_amount": ("incentive", True),
        }[mutation]
        value["event"]["attributes"][key] = replacement
    with pytest.raises(ValueError):
        credit(events)
