"""`ditto bounty`: exact terms, typed consent, signed claims, safe retries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import bittensor
import pytest

from ditto.api_models.bounty import (
    BountyBoardPage,
    BountyClaimView,
    BountyDetail,
    BountyFactView,
    BountyFeeReceipt,
    BountyObligationView,
    BountyRefundView,
    BountyRevisionView,
    BountySlotCounts,
    BountySummary,
    BountyTerms,
    bounty_appeal_message,
    bounty_claim_message,
    bounty_reason_digest,
    bounty_submit_message,
    bounty_team_digest,
    bounty_withdraw_message,
    simulated_fee_block_hash,
)
from ditto.api_models.contributor_identity import ClaStatus
from ditto.miner_cli.__main__ import _build_parser, main
from ditto.miner_cli.commands import bounty
from ditto.miner_cli.errors import TransientApiError

HOTKEY = bittensor.Keypair.create_from_uri("//Alice")
COLDKEY = bittensor.Keypair.create_from_uri("//Alice//cold")
REPO, ISSUE, DIGEST = "ditto-assistant/ditto-subnet", 2942, "ab" * 32
PHRASE = f"CLAIM BOUNTY {REPO}#{ISSUE} REVISION 1 {DIGEST}"
NOW = datetime.now(UTC)
TERMS = BountyTerms(
    title="TEST FIXTURE bounty",
    scope="Fixture scope.",
    public_interfaces="Fixture interface.",
    acceptance_evidence="Fixture tests pass.",
    reviewer_login="reviewer-fixture",
    entry_fee_alpha_rao=2_500_000_000,
    reward_pool_alpha_rao=100_000_000_000,
    funding_mode="rehearsal",
    fee_escrow_coldkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    fee_escrow_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
)


def detail(**summary_over) -> BountyDetail:
    terms = TERMS.model_copy(
        update={"funding_mode": summary_over.get("funding_mode", "rehearsal")}
    )
    summary = BountySummary(
        bounty_id=uuid4(),
        repo=REPO,
        issue=ISSUE,
        title=terms.title,
        current_revision=1,
        spec_digest=DIGEST,
        entry_fee_alpha_rao=terms.entry_fee_alpha_rao,
        reward_pool_alpha_rao=terms.reward_pool_alpha_rao,
        delivery_days=5,
        review_days=7,
        reviewer_login="reviewer-fixture",
        funding_mode=terms.funding_mode,
        current_round=1,
        claims_paused=summary_over.get("claims_paused", False),
        slots=BountySlotCounts(
            open=3, held=0, claimed=0, submitted=0, rework=0, closed=0
        ),
        reviews_overdue=0,
        awarded_alpha_rao=0,
        published_at=NOW,
        updated_at=NOW,
    )
    return BountyDetail(
        summary=summary,
        revisions=[
            BountyRevisionView(
                revision=1, spec_digest=DIGEST, terms=terms, published_at=NOW
            )
        ],
        rounds=[],
        slots=[],
        events=[],
    )


def claim_view(state: str = "held", **over) -> BountyClaimView:
    claim_id = over.pop("claim_id", uuid4())
    receipt = BountyFeeReceipt(
        block_hash=simulated_fee_block_hash(claim_id), block_number=0, extrinsic_index=0
    )
    base = {
        "claim_id": claim_id,
        "bounty_id": uuid4(),
        "repo": REPO,
        "issue": ISSUE,
        "revision": 1,
        "spec_digest": DIGEST,
        "slot_id": uuid4(),
        "round": 1,
        "position": 1,
        "state": state,
        "slot_status": "claimed" if state == "claimed" else "held",
        "hotkey": HOTKEY.ss58_address,
        "payee_coldkey": COLDKEY.ss58_address,
        "github_login": "alice-dev",
        "funding_mode": "rehearsal",
        "fee_alpha_rao": TERMS.entry_fee_alpha_rao,
        "fee_escrow_coldkey": TERMS.fee_escrow_coldkey,
        "fee_escrow_hotkey": TERMS.fee_escrow_hotkey,
        "held_at": NOW,
        "hold_expires_at": NOW + timedelta(minutes=30),
        "claimed_at": NOW if state == "claimed" else None,
        "deadline_at": NOW + timedelta(days=5) if state == "claimed" else None,
        "simulated_receipt": receipt,
        "receipts": [receipt] if state != "held" else [],
        "refunds": [],
        "submissions": [],
        "appeals": [],
        "obligations": [],
        "facts": [],
    }
    base.update(over)
    return BountyClaimView(**base)


def api(found: BountyDetail | None = None):
    client = MagicMock()
    client.__enter__.return_value = client
    found = found or detail()
    client.list_bounties.return_value = BountyBoardPage(
        items=[found.summary], total=1, limit=1, offset=0
    )
    client.get_bounty.return_value = found
    client.get_cla_status.return_value = ClaStatus(netuid=118, mode="off")
    held = claim_view("held")
    client.hold_bounty_claim.return_value = held
    client.confirm_bounty_claim.side_effect = lambda claim_id, _body: claim_view(
        "claimed", claim_id=claim_id
    )
    return client


def wallet():
    live = MagicMock()
    live.hotkey.ss58_address = HOTKEY.ss58_address
    live.hotkey.sign.side_effect = HOTKEY.sign
    live.coldkey.ss58_address = COLDKEY.ss58_address
    live.coldkey.sign.side_effect = COLDKEY.sign
    live.coldkeypub.ss58_address = COLDKEY.ss58_address
    return SimpleNamespace(hotkey_ss58=HOTKEY.ss58_address), live


def run(argv: list[str], client, live=None):
    handle, wallet_live = wallet()
    with (
        patch.object(bounty, "ApiClient", return_value=client),
        patch.object(bounty, "load_wallet", return_value=(handle, live or wallet_live)),
        patch.object(bounty, "resolve_wallet_names", return_value=("w", "h")),
        patch.object(bounty.time, "sleep"),
    ):
        return main(["bounty", *argv, "--subtensor.network", "local"])


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def claim_args(*extra: str) -> list[str]:
    return ["claim", f"{REPO}#{ISSUE}", "--github-login", "alice-dev", *extra]


def test_claim_signs_the_exact_revision_after_typed_consent(home, capsys):
    client = api()
    handle, live = wallet()

    def consent():
        output = capsys.readouterr()
        # The terms and the exact phrase are shown before anything is signed.
        assert "Type exactly" in output.err and PHRASE in output.err
        assert "2.5 alpha (2500000000 rao)" in output.err
        assert "REHEARSAL" in output.err and not output.out
        live.hotkey.sign.assert_not_called()
        return PHRASE

    with patch("builtins.input", side_effect=consent):
        assert run([*claim_args(), "--json"], client, live) == 0
    body = client.hold_bounty_claim.call_args.args[0]
    assert (body.repo, body.issue, body.revision, body.spec_digest) == (
        REPO,
        ISSUE,
        1,
        DIGEST,
    )
    assert body.payee_coldkey == COLDKEY.ss58_address and body.netuid == 118
    assert body.team_digest == bounty_team_digest(
        payee_coldkey=COLDKEY.ss58_address,
        members=[(HOTKEY.ss58_address, "alice-dev")],
    )
    assert HOTKEY.verify(
        bounty_claim_message(
            netuid=118,
            repo=REPO,
            issue=ISSUE,
            revision=1,
            spec_digest=DIGEST,
            hotkey=HOTKEY.ss58_address,
            payee_coldkey=COLDKEY.ss58_address,
            github_login="alice-dev",
            team_digest=body.team_digest,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind="hotkey",
            signer=HOTKEY.ss58_address,
        ),
        bytes.fromhex(body.proof.signature),
    )
    # The rehearsal fee is the hold's own simulated receipt.
    held = client.hold_bounty_claim.return_value
    claim_id, confirm = client.confirm_bounty_claim.call_args.args
    assert claim_id == held.claim_id
    assert confirm.receipt == held.simulated_receipt
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "claimed"
    saved = home / ".cache/ditto/bounty/local"
    terms = json.loads(
        (saved / "ditto-assistant-ditto-subnet-2942-r1.json").read_text()
    )
    assert terms["spec_digest"] == DIGEST and terms["terms"]["title"] == TERMS.title
    assert (saved / "claims" / f"{held.claim_id}.json").exists()


@pytest.mark.parametrize(
    "typed", ["", "yes", PHRASE.replace("REVISION 1", "REVISION 2")]
)
def test_a_mismatched_confirmation_signs_nothing(capsys, typed):
    client = api()
    handle, live = wallet()
    assert run([*claim_args(), "--confirmation", typed], client, live) == 1
    live.hotkey.sign.assert_not_called()
    client.hold_bounty_claim.assert_not_called()
    assert "not signed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "found,message",
    [
        (detail(funding_mode="live"), "live bounty fees are not enabled"),
        (detail(claims_paused=True), "paused"),
    ],
)
def test_unclaimable_bounties_stop_before_the_wallet(capsys, found, message):
    client = api(found)
    with patch.object(bounty, "load_wallet") as load:
        assert run([*claim_args(), "--confirmation", PHRASE], client) == 1
    load.assert_not_called()
    assert message in capsys.readouterr().err


def test_a_coldkey_proof_is_signed_by_the_payee():
    client = api()
    handle, live = wallet()
    assert (
        run(
            [*claim_args("--key-kind", "coldkey"), "--confirmation", PHRASE],
            client,
            live,
        )
        == 0
    )
    body = client.hold_bounty_claim.call_args.args[0]
    assert (body.proof.key_kind, body.proof.signer) == ("coldkey", COLDKEY.ss58_address)
    live.hotkey.sign.assert_not_called()


def test_transient_failures_resend_the_same_signed_claim(capsys):
    client = api()
    held = client.hold_bounty_claim.return_value
    client.hold_bounty_claim.side_effect = [
        TransientApiError("bounty claim failed: HTTP 503"),
        held,
    ]
    assert run([*claim_args(), "--confirmation", PHRASE], client) == 0
    first, second = (call.args[0] for call in client.hold_bounty_claim.call_args_list)
    # The retry is the same nonce and signature, so it can never claim twice.
    assert first == second
    assert "retrying" in capsys.readouterr().err


def test_a_refunded_fee_is_reported_and_exits_nonzero(capsys):
    client = api()
    client.confirm_bounty_claim.side_effect = lambda claim_id, body: claim_view(
        "refund_owed",
        claim_id=claim_id,
        refunds=[
            BountyRefundView(
                obligation_id=uuid4(),
                reason="hold_expired",
                payee_coldkey=COLDKEY.ss58_address,
                amount_alpha_rao=TERMS.entry_fee_alpha_rao,
                receipt=body.receipt,
                simulated=True,
                created_at=NOW,
            )
        ],
    )
    assert run([*claim_args(), "--confirmation", PHRASE], client) == 1
    out = capsys.readouterr().out
    assert "refund_owed" in out and "Refund owed (simulated): 2.5 alpha" in out


def test_confirm_resends_only_a_live_hold():
    client = api()
    held = claim_view("held")
    client.get_bounty_claim.return_value = held
    assert run(["confirm", str(held.claim_id)], client) == 0
    assert (
        client.confirm_bounty_claim.call_args.args[1].receipt == held.simulated_receipt
    )
    client.confirm_bounty_claim.reset_mock()
    client.get_bounty_claim.return_value = claim_view("claimed")
    assert run(["confirm", str(held.claim_id)], client) == 0
    client.confirm_bounty_claim.assert_not_called()


def test_status_list_and_show(capsys):
    client = api()
    client.get_bounty_claim.return_value = claim_view("claimed")
    assert run(["status", str(uuid4())], client) == 0
    assert "Delivery deadline" in capsys.readouterr().out
    assert run(["list"], client) == 0
    assert f"{REPO}#{ISSUE}  r1  fee 2.5 alpha" in capsys.readouterr().out
    assert run(["show", f"{REPO}#{ISSUE}"], client) == 0
    assert "reviewer @reviewer-fixture" in capsys.readouterr().out
    client.list_bounties.assert_called_with(repo=REPO, issue=ISSUE, limit=1)


def test_issue_numbers_above_the_protocol_limit_are_refused_locally(capsys):
    client = api()
    assert run(["show", f"{REPO}#2147483648"], client) == 1
    assert "above the largest issue number" in capsys.readouterr().err
    client.list_bounties.assert_not_called()
    assert run(["show", f"{REPO}#2147483647"], client) == 0


def test_list_reads_every_page_of_the_board(capsys):
    client = api()
    summary = detail().summary
    later = summary.model_copy(update={"issue": ISSUE + 1, "title": "Later bounty"})
    client.list_bounties.side_effect = lambda *, limit, offset: BountyBoardPage(
        items=[summary] * 100 if offset == 0 else [later],
        total=101,
        limit=limit,
        offset=offset,
    )
    assert run(["list"], client) == 0
    out = capsys.readouterr().out
    assert out.count(f"{REPO}#{ISSUE}  ") == 100
    assert f"{REPO}#{ISSUE + 1}  " in out and "Later bounty" in out
    assert [c.kwargs["offset"] for c in client.list_bounties.call_args_list] == [0, 100]
    assert run(["list", "--json"], client) == 0
    whole = json.loads(capsys.readouterr().out)
    assert (len(whole["items"]), whole["total"], whole["limit"]) == (101, 101, 101)


def test_a_board_that_ends_before_its_total_is_an_error(capsys):
    client = api()
    summary = detail().summary
    client.list_bounties.side_effect = lambda *, limit, offset: BountyBoardPage(
        items=[summary] if offset == 0 else [], total=3, limit=limit, offset=offset
    )
    assert run(["list"], client) == 1
    assert "ended early: 1 of 3" in capsys.readouterr().err


def test_a_bounty_without_a_revision_is_refused(capsys):
    client = api()
    client.get_bounty.return_value = detail().model_copy(update={"revisions": []})
    assert run(["show", f"{REPO}#{ISSUE}"], client) == 1
    assert "no published revision" in capsys.readouterr().err


def test_an_empty_board_lists_as_an_empty_page(capsys):
    client = api()
    client.list_bounties.side_effect = lambda *, limit, offset: BountyBoardPage(
        items=[], total=0, limit=limit, offset=offset
    )
    assert run(["list", "--json"], client) == 0
    empty = json.loads(capsys.readouterr().out)
    assert (empty["items"], empty["total"], empty["limit"]) == ([], 0, 100)


@pytest.mark.parametrize(
    "target", ["ditto-assistant/ditto-subnet", "Ditto-Assistant/ditto-subnet#1", "#1"]
)
def test_targets_must_be_canonical_issue_references(capsys, target):
    client = api()
    assert run(["claim", target, "--github-login", "alice-dev"], client) == 1
    assert "not an issue reference" in capsys.readouterr().err
    client.hold_bounty_claim.assert_not_called()


def test_a_failed_local_save_never_loses_the_claim(home, capsys):
    client = api()
    with (
        patch("builtins.input", return_value=PHRASE),
        patch.object(bounty, "_save", side_effect=[home, OSError("disk full")]),
    ):
        assert run(claim_args(), client) == 0
    err = capsys.readouterr().err
    held = client.hold_bounty_claim.return_value
    assert f"(claim {held.claim_id})" in err and "disk full" in err
    # The fee is still confirmed.
    client.confirm_bounty_claim.assert_called_once()


@pytest.mark.parametrize("state", ["refund_owed", "hold_expired"])
def test_confirm_exits_non_zero_unless_the_fee_claimed_the_slot(state):
    client = api()
    client.get_bounty_claim.return_value = claim_view(state)
    assert run(["confirm", str(uuid4())], client) == 1
    assert run(["status", str(uuid4())], client) == 0


def test_claim_requires_a_github_login():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["bounty", "claim", f"{REPO}#{ISSUE}"])


SHA = "3f" * 20


def acting_api(state: str = "claimed"):
    client = api()
    view = claim_view(state)
    client.get_bounty_claim.return_value = view
    for method in (
        "submit_bounty_work",
        "withdraw_bounty_claim",
        "appeal_bounty_decision",
    ):
        getattr(client, method).return_value = view
    return client, view


def test_submit_signs_the_exact_commit_after_typed_consent(capsys):
    client, view = acting_api()
    phrase = f"SUBMIT BOUNTY CLAIM {view.claim_id} PR 61 {SHA}"
    argv = ["submit", str(view.claim_id), "--pr", "61", "--head-sha", SHA.upper()]
    assert run([*argv, "--confirmation", phrase], client) == 0
    claim_id, body = client.submit_bounty_work.call_args.args
    assert claim_id == view.claim_id
    assert (body.repo, body.pr_number, body.head_sha) == (REPO, 61, SHA)
    assert HOTKEY.verify(
        bounty_submit_message(
            netuid=118,
            claim_id=view.claim_id,
            hotkey=HOTKEY.ss58_address,
            repo=REPO,
            pr_number=61,
            head_sha=SHA,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind="hotkey",
            signer=HOTKEY.ss58_address,
        ),
        bytes.fromhex(body.proof.signature),
    )
    assert "exact commit" in capsys.readouterr().err


def test_withdraw_states_the_retained_fee_and_needs_its_phrase(capsys):
    client, view = acting_api()
    assert run(["withdraw", str(view.claim_id), "--confirmation", "yes"], client) == 1
    client.withdraw_bounty_claim.assert_not_called()
    err = capsys.readouterr().err
    assert "retained" in err and "nothing was signed" in err
    phrase = f"WITHDRAW BOUNTY CLAIM {view.claim_id}"
    assert run(["withdraw", str(view.claim_id), "--confirmation", phrase], client) == 0
    body = client.withdraw_bounty_claim.call_args.args[1]
    assert HOTKEY.verify(
        bounty_withdraw_message(
            netuid=118,
            claim_id=view.claim_id,
            hotkey=HOTKEY.ss58_address,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind="hotkey",
            signer=HOTKEY.ss58_address,
        ),
        bytes.fromhex(body.proof.signature),
    )


def test_appeal_binds_the_statement_digest(tmp_path):
    client, view = acting_api("claimed")
    statement = tmp_path / "appeal.md"
    statement.write_text("The deadline fell during a fixture outage.\n")
    argv = ["appeal", str(view.claim_id), "--event", "42", "--reason-file"]
    # The phrase names the claim as well as the event, like submit and withdraw.
    unbound = "APPEAL BOUNTY EVENT 42"
    assert run([*argv, str(statement), "--confirmation", unbound], client) == 1
    client.appeal_bounty_decision.assert_not_called()
    phrase = f"APPEAL BOUNTY CLAIM {view.claim_id} EVENT 42"
    assert run([*argv, str(statement), "--confirmation", phrase], client) == 0
    body = client.appeal_bounty_decision.call_args.args[1]
    assert body.reason == statement.read_text() and body.subject_event_id == 42
    assert HOTKEY.verify(
        bounty_appeal_message(
            netuid=118,
            claim_id=view.claim_id,
            hotkey=HOTKEY.ss58_address,
            subject_event_id=42,
            reason_digest=bounty_reason_digest(statement.read_text()),
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind="hotkey",
            signer=HOTKEY.ss58_address,
        ),
        bytes.fromhex(body.proof.signature),
    )
    statement.write_text("   \n")
    assert run([*argv, str(statement), "--confirmation", phrase], client) == 1


def test_actions_refuse_another_wallets_claim_and_a_foreign_coldkey(capsys):
    client, view = acting_api()
    other = bittensor.Keypair.create_from_uri("//Mallory")
    stranger = claim_view("claimed", claim_id=view.claim_id, hotkey=other.ss58_address)
    client.get_bounty_claim.return_value = stranger
    phrase = f"WITHDRAW BOUNTY CLAIM {view.claim_id}"
    assert run(["withdraw", str(view.claim_id), "--confirmation", phrase], client) == 1
    assert "belongs to hotkey" in capsys.readouterr().err
    client.get_bounty_claim.return_value = claim_view(
        "claimed", claim_id=view.claim_id, payee_coldkey=other.ss58_address
    )
    assert (
        run(
            [
                "withdraw",
                str(view.claim_id),
                "--key-kind",
                "coldkey",
                "--confirmation",
                phrase,
            ],
            client,
        )
        == 1
    )
    assert "payee of record" in capsys.readouterr().err
    client.withdraw_bounty_claim.assert_not_called()


def test_status_shows_awards_refunds_and_their_settlement(capsys):
    client = api()
    view = claim_view("claimed")
    obligations = [
        BountyObligationView(
            obligation_id=uuid4(),
            kind=kind,
            reason=reason,
            payee_coldkey=COLDKEY.ss58_address,
            amount_alpha_rao=amount,
            state=state,
            round=1,
            approvals=0,
            approvals_required=1,
            attribution=None,
            simulated=True,
            created_at=NOW,
            transfers=[],
        )
        for kind, reason, amount, state in (
            ("fee_refund", "accepted", TERMS.entry_fee_alpha_rao, "confirmed"),
            ("award", "accepted_work", 40_000_000_000, "awaiting_approval"),
        )
    ]
    client.get_bounty_claim.return_value = view.model_copy(
        update={
            "obligations": obligations,
            "facts": [BountyFactView(kind="merged", reference=SHA, recorded_at=NOW)],
        }
    )
    assert run(["status", str(view.claim_id)], client) == 0
    out = capsys.readouterr().out
    assert "Refund owed (simulated): 2.5 alpha" in out and "confirmed" in out
    assert "Award (simulated): 40 alpha" in out and "awaiting approval" in out
    assert f"Merged: {SHA}" in out
