from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.bounty import (
    MAX_ALPHA_RAO,
    SPEC_V1_FIELDS,
    BountyAwardRequest,
    BountyClaimRequest,
    BountyFeeReceipt,
    BountyPublishRequest,
    BountySubmitRequest,
    BountyTerms,
    bounty_appeal_decision_confirmation,
    bounty_appeal_message,
    bounty_approval_confirmation,
    bounty_award_confirmation,
    bounty_claim_message,
    bounty_dispatch_confirmation,
    bounty_fact_confirmation,
    bounty_pause_confirmation,
    bounty_payout_policy_confirmation,
    bounty_publish_confirmation,
    bounty_reason_digest,
    bounty_reassign_confirmation,
    bounty_reconcile_confirmation,
    bounty_review_confirmation,
    bounty_slot_confirmation,
    bounty_spec_digest,
    bounty_submit_message,
    bounty_team_digest,
    bounty_withdraw_message,
    simulated_fee_block_hash,
    simulated_transfer_block_hash,
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
        {"dependencies": ["ditto-assistant/ditto-subnet#2147483648"]},
        {"reviewer_login": "has space"},
        {"funding_mode": "simulated"},
        {"fee_escrow_hotkey": "not-an-address"},
    ],
)
def test_terms_reject_coerced_or_invalid_values(change):
    with pytest.raises(ValidationError):
        BountyTerms.model_validate({**TERMS, **change})


def test_a_dependency_names_any_issue_an_issue_number_can():
    terms = BountyTerms.model_validate(
        {**TERMS, "dependencies": ["ditto-assistant/ditto-subnet#2147483647"]}
    )
    assert terms.dependencies == ["ditto-assistant/ditto-subnet#2147483647"]


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


def test_simulated_receipt_is_bound_to_one_claim_and_its_signature():
    claim = UUID("00000000-0000-4000-8000-000000000001")
    signature = "AB" * 64
    expected = hashlib.sha256(
        f"ditto-bounty-simulated-fee:v1:{claim}:{'ab' * 64}".encode()
    )
    assert simulated_fee_block_hash(claim, signature) == "0x" + expected.hexdigest()
    # Any case of the same signature derives the same receipt.
    assert simulated_fee_block_hash(claim, signature.lower()) == (
        simulated_fee_block_hash(claim, signature)
    )
    # The claim id alone, which the public board shows, never derives it.
    assert simulated_fee_block_hash(claim, signature) != simulated_fee_block_hash(
        NONCE, signature
    )
    assert simulated_fee_block_hash(claim, signature) != simulated_fee_block_hash(
        claim, "cd" * 64
    )


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


CLAIM = UUID("00000000-0000-4000-8000-0000000000aa")


def test_submit_message_binds_the_exact_commit():
    message = bounty_submit_message(
        netuid=118,
        claim_id=CLAIM,
        hotkey=HOTKEY,
        repo="ditto-assistant/ditto-subnet",
        pr_number=61,
        head_sha="3f" * 20,
        nonce=NONCE,
        issued_at=ISSUED,
        key_kind="coldkey",
        signer=COLDKEY,
    )
    assert (
        message
        == (
            f"ditto-bounty-submit:v1:118:{CLAIM}:{HOTKEY}:ditto-assistant/ditto-subnet:"
            f"61:{'3f' * 20}:{NONCE}:2026-09-21T12:00:00.000000+00:00:coldkey:{COLDKEY}"
        ).encode()
    )


def test_withdraw_and_appeal_messages():
    assert (
        bounty_withdraw_message(
            netuid=118,
            claim_id=CLAIM,
            hotkey=HOTKEY,
            nonce=NONCE,
            issued_at=ISSUED,
            key_kind="hotkey",
            signer=HOTKEY,
        )
        == (
            f"ditto-bounty-withdraw:v1:118:{CLAIM}:{HOTKEY}:{NONCE}:"
            f"2026-09-21T12:00:00.000000+00:00:hotkey:{HOTKEY}"
        ).encode()
    )
    digest = bounty_reason_digest("The deadline fell during an outage.")
    assert digest == hashlib.sha256(b"The deadline fell during an outage.").hexdigest()
    assert (
        bounty_appeal_message(
            netuid=118,
            claim_id=CLAIM,
            hotkey=HOTKEY,
            subject_event_id=42,
            reason_digest=digest,
            nonce=NONCE,
            issued_at=ISSUED,
            key_kind="hotkey",
            signer=HOTKEY,
        )
        == (
            f"ditto-bounty-appeal:v1:118:{CLAIM}:{HOTKEY}:42:{digest}:{NONCE}:"
            f"2026-09-21T12:00:00.000000+00:00:hotkey:{HOTKEY}"
        ).encode()
    )


@pytest.mark.parametrize("sha", ["3F" * 20, "3f" * 19, "3f" * 21, "zz" * 20])
def test_submissions_need_a_full_lowercase_commit_sha(sha):
    with pytest.raises(ValidationError):
        BountySubmitRequest.model_validate(
            {
                "netuid": 118,
                "claim_id": str(CLAIM),
                "hotkey": HOTKEY,
                "repo": "ditto-assistant/ditto-subnet",
                "pr_number": 61,
                "head_sha": sha,
                "nonce": str(NONCE),
                "issued_at": "2026-09-21T12:00:00+00:00",
                "proof": {
                    "key_kind": "hotkey",
                    "signer": HOTKEY,
                    "signature": "ab" * 64,
                },
            }
        )


def test_operator_confirmations_name_their_target():
    repo, issue = "ditto-assistant/ditto-subnet", 2942
    assert (
        bounty_review_confirmation(
            repo=repo, issue=issue, claim_id=CLAIM, decision="changes_requested"
        )
        == f"REQUEST CHANGES ON BOUNTY CLAIM {repo}#{issue} {CLAIM}"
    )
    assert (
        bounty_slot_confirmation(
            action="cancel", repo=repo, issue=issue, round_number=1, position=3
        )
        == f"CANCEL BOUNTY SLOT {repo}#{issue} ROUND 1 SLOT 3"
    )
    assert bounty_appeal_decision_confirmation(appeal_id=CLAIM, upheld=False) == (
        f"DENY BOUNTY APPEAL {CLAIM}"
    )
    assert bounty_reassign_confirmation(repo=repo, issue=issue) == (
        f"REASSIGN BOUNTY REVIEWER {repo}#{issue}"
    )


def test_settlement_confirmations_and_simulated_transfers():
    assert bounty_award_confirmation(claim_id=CLAIM, amount_alpha_rao=5) == (
        f"AWARD BOUNTY CLAIM {CLAIM} 5 ALPHA RAO"
    )
    assert (
        bounty_approval_confirmation(
            obligation_id=CLAIM, round_number=2, amount_alpha_rao=7, payee=COLDKEY
        )
        == f"APPROVE BOUNTY OBLIGATION {CLAIM} ROUND 2 7 ALPHA RAO TO {COLDKEY}"
    )
    assert bounty_dispatch_confirmation(obligation_id=CLAIM, round_number=1) == (
        f"DISPATCH BOUNTY OBLIGATION {CLAIM} ROUND 1"
    )
    assert bounty_reconcile_confirmation(transfer_id=CLAIM) == (
        f"RECONCILE BOUNTY TRANSFER {CLAIM}"
    )
    assert bounty_payout_policy_confirmation(revision=3) == "SET BOUNTY PAYOUT POLICY 3"
    assert bounty_fact_confirmation(claim_id=CLAIM, kind="merged") == (
        f"RECORD BOUNTY MERGED {CLAIM}"
    )
    expected = hashlib.sha256(
        f"ditto-bounty-simulated-transfer:v1:{CLAIM}".encode()
    ).hexdigest()
    assert simulated_transfer_block_hash(CLAIM) == "0x" + expected
    # A transfer receipt is never mistaken for a fee receipt.
    assert simulated_transfer_block_hash(CLAIM) != simulated_fee_block_hash(
        CLAIM, "ab" * 64
    )


@pytest.mark.parametrize(
    "change",
    [
        {"amount_alpha_rao": 0},
        {"amount_alpha_rao": "5"},
        {"attribution": ""},
        {"attribution": "x" * 2001},
        {"reason": "short"},
    ],
)
def test_award_requests_are_exact(change):
    valid = {
        "amount_alpha_rao": 5,
        "attribution": "Fixture credit",
        "reason": "fixture award decision",
        "confirmation": "x",
    }
    BountyAwardRequest.model_validate(valid)
    with pytest.raises(ValidationError):
        BountyAwardRequest.model_validate({**valid, **change})
