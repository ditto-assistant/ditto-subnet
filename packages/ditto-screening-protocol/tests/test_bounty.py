from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.bounty import (
    MAX_ALPHA_RAO,
    SPEC_V1_FIELDS,
    BountyClaimRequest,
    BountyFeeReceipt,
    BountyPublishRequest,
    BountyTerms,
    bounty_claim_message,
    bounty_pause_confirmation,
    bounty_publish_confirmation,
    bounty_spec_digest,
    bounty_team_digest,
    simulated_fee_block_hash,
)

COLDKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
TERMS = {
    "title": "TEST FIXTURE bounty",
    "scope": "Fixture scope. Not real work.",
    "public_interfaces": "Fixture interface.",
    "acceptance_evidence": "Fixture tests pass.",
    "dependencies": ["ditto-assistant/ditto-subnet#2889"],
    "reviewer_login": "reviewer-fixture",
    "entry_fee_alpha_rao": 2_000_000_000,
    "reward_pool_alpha_rao": 100_000_000_000,
    "funding_mode": "rehearsal",
    "fee_escrow_coldkey": COLDKEY,
    "fee_escrow_hotkey": HOTKEY,
}


def digest(terms: dict | None = None, **identity) -> str:
    return bounty_spec_digest(
        repo=identity.get("repo", "ditto-assistant/ditto-subnet"),
        issue=identity.get("issue", 2942),
        revision=identity.get("revision", 1),
        terms=BountyTerms.model_validate(terms or TERMS),
    )


def test_spec_digest_is_pinned():
    # Contributors sign over this digest; it must never drift.
    assert (
        digest() == "171acd696fa78cc0ea0eb400436869d603b957a6574822f9f6674b7583f14ac4"
    )


def test_spec_digest_hashes_canonical_json_bytes():
    canonical = (
        '{"domain":"ditto-bounty-spec:v1","issue":2942,'
        '"repo":"ditto-assistant/ditto-subnet","revision":1,"terms":{'
        '"acceptance_evidence":"Fixture tests pass.","attempts_per_round":3,'
        '"delivery_days":5,"dependencies":["ditto-assistant/ditto-subnet#2889"],'
        '"entry_fee_alpha_rao":2000000000,'
        f'"fee_escrow_coldkey":"{COLDKEY}","fee_escrow_hotkey":"{HOTKEY}",'
        '"funding_mode":"rehearsal","hold_minutes":30,'
        '"public_interfaces":"Fixture interface.","review_days":7,'
        '"reviewer_login":"reviewer-fixture","reward_pool_alpha_rao":100000000000,'
        '"scope":"Fixture scope. Not real work.","title":"TEST FIXTURE bounty"}}'
    ).encode()
    assert digest() == hashlib.sha256(canonical).hexdigest()


def test_spec_digest_ignores_input_order_and_unknown_fields():
    reordered = dict(reversed(list(TERMS.items())))
    assert digest(reordered) == digest()
    assert digest({**TERMS, "future_field": "ignored"}) == digest()


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Another title"},
        {"scope": "Fixture scope. Not real work!"},
        {"public_interfaces": "Other interface."},
        {"acceptance_evidence": "Other evidence."},
        {"dependencies": []},
        {"reviewer_login": "other-reviewer"},
        {"entry_fee_alpha_rao": 2_000_000_001},
        {"reward_pool_alpha_rao": 99_999_999_999},
        {"attempts_per_round": 2},
        {"delivery_days": 6},
        {"review_days": 8},
        {"hold_minutes": 31},
        {"funding_mode": "live"},
        {"fee_escrow_coldkey": HOTKEY},
        {"fee_escrow_hotkey": COLDKEY},
    ],
)
def test_every_signed_term_changes_the_digest(change):
    assert digest({**TERMS, **change}) != digest()


@pytest.mark.parametrize(
    "identity", [{"repo": "ditto-assistant/other"}, {"issue": 2943}, {"revision": 2}]
)
def test_bounty_identity_and_revision_change_the_digest(identity):
    assert digest(**identity) != digest()


def test_signed_field_list_matches_the_terms_model():
    # A new term needs a new spec domain version, never a silent change.
    assert set(SPEC_V1_FIELDS) == set(BountyTerms.model_fields)


@pytest.mark.parametrize(
    "change",
    [
        {"entry_fee_alpha_rao": "2000000000"},
        {"entry_fee_alpha_rao": 2.0e9},
        {"entry_fee_alpha_rao": True},
        {"entry_fee_alpha_rao": -1},
        {"entry_fee_alpha_rao": MAX_ALPHA_RAO + 1},
        {"reward_pool_alpha_rao": MAX_ALPHA_RAO + 1},
        {"attempts_per_round": "3"},
        {"title": ""},
        {"dependencies": ["not a reference"]},
        {"dependencies": ["Ditto-Assistant/ditto-subnet#2889"]},
        {"dependencies": ["ditto-assistant/ditto-subnet#0"]},
        {"reviewer_login": "has space"},
        {"funding_mode": "simulated"},
        {"fee_escrow_hotkey": "not-an-address"},
    ],
)
def test_terms_reject_coerced_or_invalid_values(change):
    with pytest.raises(ValidationError):
        BountyTerms.model_validate({**TERMS, **change})


def test_amounts_stay_exact_in_every_json_client():
    # JavaScript numbers hold integers exactly up to 2^53 - 1.
    assert MAX_ALPHA_RAO == 9_007_199_254_740_991
    terms = BountyTerms.model_validate(
        {**TERMS, "reward_pool_alpha_rao": MAX_ALPHA_RAO}
    )
    assert terms.reward_pool_alpha_rao == MAX_ALPHA_RAO


@pytest.mark.parametrize(
    "repo",
    [
        "ditto-assistant",
        "a:b/c",
        "owner/repo#1",
        "-x/y",
        "Ditto-Assistant/ditto-subnet",
    ],
)
def test_publish_request_rejects_ambiguous_repositories(repo):
    with pytest.raises(ValidationError):
        BountyPublishRequest.model_validate(
            {
                "repo": repo,
                "issue": 1,
                "expected_revision": 0,
                "terms": TERMS,
                "reviewer_actor": "operator@example.com",
                "reason": "fixture publication",
                "confirmation": "x",
            }
        )


def test_publish_confirmation_names_the_exact_revision():
    assert (
        bounty_publish_confirmation(
            repo="ditto-assistant/ditto-subnet",
            issue=2942,
            revision=1,
            spec_digest="ab" * 32,
        )
        == "PUBLISH BOUNTY ditto-assistant/ditto-subnet#2942 REVISION 1 " + "ab" * 32
    )


NONCE = UUID("1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b")
ISSUED = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_team_digest_is_canonical_json_of_the_sorted_team():
    canonical = (
        '{"members":[{"github_login":"alice-dev","hotkey":"'
        + HOTKEY
        + '"}],"payee_coldkey":"'
        + COLDKEY
        + '"}'
    ).encode()
    assert (
        bounty_team_digest(payee_coldkey=COLDKEY, members=[(HOTKEY, "alice-dev")])
        == hashlib.sha256(canonical).hexdigest()
    )
    # Member order never changes the team.
    pair = [(HOTKEY, "alice-dev"), (COLDKEY, "bob-dev")]
    assert bounty_team_digest(
        payee_coldkey=COLDKEY, members=pair
    ) == bounty_team_digest(payee_coldkey=COLDKEY, members=pair[::-1])


def test_claim_message_signs_every_field_in_order():
    team = "cd" * 32
    message = bounty_claim_message(
        netuid=118,
        repo="ditto-assistant/ditto-subnet",
        issue=2942,
        revision=1,
        spec_digest="ab" * 32,
        hotkey=HOTKEY,
        payee_coldkey=COLDKEY,
        github_login="alice-dev",
        team_digest=team,
        nonce=NONCE,
        issued_at=ISSUED,
        key_kind="hotkey",
        signer=HOTKEY,
    )
    assert (
        message
        == (
            "ditto-bounty-claim:v1:118:ditto-assistant/ditto-subnet:2942:1:"
            f"{'ab' * 32}:"
            f"{HOTKEY}:{COLDKEY}:alice-dev:{team}:{NONCE}:"
            f"2026-09-21T12:00:00.000000+00:00:hotkey:{HOTKEY}"
        ).encode()
    )


def test_claim_message_requires_an_aware_time():
    with pytest.raises(ValueError):
        bounty_claim_message(
            netuid=118,
            repo="ditto-assistant/ditto-subnet",
            issue=1,
            revision=1,
            spec_digest="ab" * 32,
            hotkey=HOTKEY,
            payee_coldkey=COLDKEY,
            github_login="alice-dev",
            team_digest="cd" * 32,
            nonce=NONCE,
            issued_at=datetime(2026, 9, 21, 12, 0),
            key_kind="hotkey",
            signer=HOTKEY,
        )


def test_simulated_receipt_is_bound_to_one_claim():
    claim = UUID("00000000-0000-4000-8000-000000000001")
    expected = hashlib.sha256(f"ditto-bounty-simulated-fee:v1:{claim}".encode())
    assert simulated_fee_block_hash(claim) == "0x" + expected.hexdigest()
    assert simulated_fee_block_hash(claim) != simulated_fee_block_hash(NONCE)


@pytest.mark.parametrize(
    "change",
    [
        {"revision": 0},
        {"revision": "1"},
        {"netuid": "118"},
        {"spec_digest": "AB" * 32},
        {"payee_coldkey": "not-an-address"},
        {"issued_at": "2026-09-21T12:00:00"},
        {"repo": "Ditto-Assistant/ditto-subnet"},
    ],
)
def test_claim_request_rejects_invalid_fields(change):
    valid = {
        "netuid": 118,
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2942,
        "revision": 1,
        "spec_digest": "ab" * 32,
        "hotkey": HOTKEY,
        "payee_coldkey": COLDKEY,
        "github_login": "alice-dev",
        "team_digest": "cd" * 32,
        "nonce": str(NONCE),
        "issued_at": "2026-09-21T12:00:00+00:00",
        "proof": {"key_kind": "hotkey", "signer": HOTKEY, "signature": "ab" * 64},
    }
    BountyClaimRequest.model_validate(valid)
    with pytest.raises(ValidationError):
        BountyClaimRequest.model_validate({**valid, **change})


@pytest.mark.parametrize(
    "receipt",
    [
        {"block_hash": "0x" + "AB" * 32, "block_number": 1, "extrinsic_index": 0},
        {"block_hash": "ab" * 32, "block_number": 1, "extrinsic_index": 0},
        {"block_hash": "0x" + "ab" * 32, "block_number": -1, "extrinsic_index": 0},
        {"block_hash": "0x" + "ab" * 32, "block_number": 1, "extrinsic_index": "0"},
    ],
)
def test_fee_receipts_are_exact(receipt):
    with pytest.raises(ValidationError):
        BountyFeeReceipt.model_validate(receipt)


def test_pause_confirmation_names_the_bounty_and_direction():
    kwargs = {"repo": "ditto-assistant/ditto-subnet", "issue": 2942}
    assert (
        bounty_pause_confirmation(**kwargs, paused=True)
        == "PAUSE BOUNTY CLAIMS ditto-assistant/ditto-subnet#2942"
    )
    assert bounty_pause_confirmation(**kwargs, paused=False).startswith("RESUME ")
