"""Retained proof decoding never leaks missing-key exceptions."""

import pytest

from ditto.api_server.treasury_aggregate import parent_source_identity
from ditto.api_server.treasury_ingress import ReceiptConflict


def test_legacy_and_enriched_aggregate_parent_identity():
    identity = {
        "source_block_hash": "block",
        "source_event_digest": "event",
        "source_amount_rao": 40,
    }
    assert parent_source_identity(identity, 110) == ("block", "event", 40)
    assert parent_source_identity(
        {
            "source_claims": [
                {"source_block": 110, "epoch_index": 9, "amount_rao": 20, **identity},
            ]
        },
        110,
    ) == ("block", "event", 40)


@pytest.mark.parametrize(
    "proof",
    [
        None,
        {},
        {"source_claims": "not-a-list"},
        {"source_claims": [None]},
        {"source_claims": [{"source_block": 110, "amount_rao": 20, "epoch_index": 9}]},
        {"source_claims": [{"source_block": 111}]},
        {"source_claims": [{"source_block": 110}, {"source_block": 110}]},
    ],
)
def test_incomplete_or_ambiguous_aggregate_is_conflict(proof):
    with pytest.raises(ReceiptConflict):
        parent_source_identity(proof, 110)
