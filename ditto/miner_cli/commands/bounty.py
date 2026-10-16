"""Browse build bounties, claim a paid slot and deliver the work.

``claim`` shows the exact terms of the bounty's current revision, saves them
for review, and signs only after a typed confirmation naming that revision
and its digest. It then holds a slot, pays the entry fee and confirms it.
Every write is an exact retry, so a lost response is resent safely.

``submit`` signs the exact commit of a pull request for review, ``withdraw``
gives up a slot (the fee is retained) and ``appeal`` contests a rejection or a
missed deadline. Each names its target in a typed confirmation first.

In v1 every bounty runs in rehearsal funding: the fee is simulated, no funds
move, and Platform records any refund it would owe.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ditto.api_models.bounty import (
    BountyAppealRequest,
    BountyClaimRequest,
    BountyClaimView,
    BountyConfirmRequest,
    BountyDetail,
    BountySubmitRequest,
    BountyWithdrawRequest,
    bounty_appeal_message,
    bounty_claim_message,
    bounty_reason_digest,
    bounty_submit_message,
    bounty_team_digest,
    bounty_withdraw_message,
)
from ditto.api_models.contributor_identity import ContributorProof
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import prompt_input
from ditto.miner_cli.errors import MinerCliError, TransientApiError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

_ISSUE_REF = re.compile(
    r"^(?P<repo>[a-z0-9][a-z0-9-]{0,38}/[a-z0-9._-]{1,100})#(?P<issue>[1-9][0-9]{0,9})$"
)
_RAO_PER_ALPHA = Decimal(1_000_000_000)
_RETRY_DELAYS = (1.0, 2.0, 4.0)


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "bounty",
        help="Browse build bounties and claim a paid slot.",
        parents=parents or [],
    )
    subs = parser.add_subparsers(dest="bounty_command", required=True)
    listing = subs.add_parser("list", parents=parents or [], help="List bounties.")
    listing.add_argument("--json", action="store_true")
    show = subs.add_parser(
        "show", parents=parents or [], help="Show a bounty's terms and slots."
    )
    show.add_argument("target", help="owner/repo#issue or a bounty id")
    show.add_argument("--json", action="store_true")
    claim = subs.add_parser(
        "claim",
        parents=parents or [],
        help="Claim a paid slot on a bounty's current revision.",
    )
    claim.add_argument("target", help="owner/repo#issue")
    claim.add_argument(
        "--github-login",
        required=True,
        help="The GitHub login linked to this hotkey with `ditto link-github`.",
    )
    _signing_options(claim, confirmation=False)
    claim.add_argument(
        "--confirmation",
        help=(
            "Explicit typed consent: CLAIM BOUNTY <owner/repo#issue> REVISION <n> "
            "<digest>. No --yes shortcut."
        ),
    )
    claim.add_argument("--json", action="store_true")
    for name, text in (
        ("confirm", "Pay and confirm the fee of an existing hold."),
        ("status", "Show one of your claims."),
    ):
        sub = subs.add_parser(name, parents=parents or [], help=text)
        sub.add_argument("claim_id", type=UUID)
        sub.add_argument("--json", action="store_true")
    for name, text in (
        ("submit", "Submit the exact commit of a pull request for review."),
        ("withdraw", "Give up a claimed slot; the entry fee is retained."),
        ("appeal", "Appeal a rejection or a missed deadline of your claim."),
    ):
        sub = subs.add_parser(name, parents=parents or [], help=text)
        sub.add_argument("claim_id", type=UUID)
        _signing_options(sub)
        if name == "submit":
            sub.add_argument("--pr", type=int, required=True, dest="pr_number")
            sub.add_argument("--head-sha", required=True, help="Full 40-hex commit")
        if name == "appeal":
            sub.add_argument(
                "--event", type=int, required=True, help="The decision's event id"
            )
            sub.add_argument(
                "--reason-file",
                type=Path,
                required=True,
                help="Your appeal statement (UTF-8). It stays private unless you "
                "publish it; Platform shows only its digest.",
            )
    for sub in subs.choices.values():
        sub.set_defaults(func=run)
    return parser


def _signing_options(
    parser: argparse.ArgumentParser, *, confirmation: bool = True
) -> None:
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
    )
    parser.add_argument(
        "--key-kind",
        choices=("hotkey", "coldkey"),
        default="hotkey",
        help="Prove the action with the hotkey (default) or the payee coldkey.",
    )
    if confirmation:
        parser.add_argument(
            "--confirmation", help="The exact typed confirmation; no --yes shortcut."
        )
        parser.add_argument("--json", action="store_true")


def alpha(rao: int) -> str:
    amount = (Decimal(rao) / _RAO_PER_ALPHA).normalize()
    return f"{amount:f} alpha ({rao} rao)"


def _issue_ref(target: str) -> tuple[str, int]:
    match = _ISSUE_REF.match(target)
    if match is None:
        raise MinerCliError(
            f"{target!r} is not an issue reference: use owner/repo#issue in "
            "lowercase, for example ditto-assistant/ditto-subnet#2942"
        )
    return match["repo"], int(match["issue"])


def _find(client: ApiClient, target: str) -> BountyDetail:
    try:
        bounty_id = UUID(target)
    except ValueError:
        repo, issue = _issue_ref(target)
        page = client.list_bounties(repo=repo, issue=issue, limit=1)
        if not page.items:
            raise MinerCliError(f"no bounty is published for {repo}#{issue}") from None
        bounty_id = page.items[0].bounty_id
    return client.get_bounty(bounty_id)


def _retrying[T](send: Callable[[], T]) -> T:
    """Resend an exact-retry write through transient failures."""
    for delay in _RETRY_DELAYS:
        try:
            return send()
        except TransientApiError as exc:
            print(f"{exc}; retrying in {delay:g}s", file=sys.stderr)
            time.sleep(delay)
    return send()


def _print_json(value: Any) -> None:
    print(json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True))


def _print_claim(view: BountyClaimView) -> None:
    print(
        f"Claim {view.claim_id}: {view.state} — {view.repo}#{view.issue} revision "
        f"{view.revision}, round {view.round} slot {view.position} "
        f"({view.slot_status})"
    )
    if view.state == "held":
        print(f"Hold expires at {view.hold_expires_at.isoformat()}")
    if view.deadline_at is not None:
        print(f"Delivery deadline: {view.deadline_at.isoformat()}")
    settled = {o.obligation_id for o in view.obligations}
    for refund in view.refunds:
        if refund.obligation_id in settled:
            continue
        simulated = " (simulated)" if refund.simulated else ""
        print(
            f"Refund owed{simulated}: {alpha(refund.amount_alpha_rao)} to "
            f"{refund.payee_coldkey} ({refund.reason})"
        )
    for owed in view.obligations:
        simulated = " (simulated)" if owed.simulated else ""
        label = {
            "award": "Award",
            "fee_refund": "Refund owed",
            "fee_retained": "Fee retained",
        }[owed.kind]
        print(
            f"{label}{simulated}: {alpha(owed.amount_alpha_rao)} to "
            f"{owed.payee_coldkey} ({owed.reason}; {owed.state.replace('_', ' ')})"
        )
    for fact in view.facts:
        print(f"{fact.kind.capitalize()}: {fact.reference}")


def _confirm_fee(client: ApiClient, view: BountyClaimView) -> BountyClaimView:
    if view.state != "held":
        return view
    if view.funding_mode != "rehearsal" or view.simulated_receipt is None:
        raise MinerCliError(
            "live bounty fees are not enabled; this CLI pays rehearsal fees only"
        )
    receipt = view.simulated_receipt
    print(
        f"Paying the rehearsal fee of {alpha(view.fee_alpha_rao)}: simulated, no "
        "funds move",
        file=sys.stderr,
    )
    return _retrying(
        lambda: client.confirm_bounty_claim(
            view.claim_id, BountyConfirmRequest(receipt=receipt)
        )
    )


def _save(path: Path, document: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def _claim(client: ApiClient, args: argparse.Namespace, network_name: str) -> int:
    detail = _find(client, args.target)
    summary, current = detail.summary, detail.revisions[-1]
    netuid = network_netuid(client)
    if summary.funding_mode != "rehearsal":
        raise MinerCliError(
            "live bounty fees are not enabled; this CLI claims rehearsal bounties only"
        )
    if summary.claims_paused:
        raise MinerCliError("new claims are paused for this bounty")
    owner, name = summary.repo.split("/")
    cache = Path.home() / ".cache" / "ditto" / "bounty" / network_name
    terms_path = _save(
        cache / f"{owner}-{name}-{summary.issue}-r{current.revision}.json",
        {
            "repo": summary.repo,
            "issue": summary.issue,
            "revision": current.revision,
            "spec_digest": current.spec_digest,
            "terms": current.terms.model_dump(mode="json"),
        },
    )
    names = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty(),
    )
    handle, wallet = load_wallet(coldkey_name=names[0], hotkey_name=names[1])
    try:
        payee = wallet.coldkeypub.ss58_address
    except Exception:
        raise MinerCliError(
            "cannot read the coldkey address; check the wallet's coldkeypub file"
        ) from None
    terms = current.terms
    print(
        f"Review the exact terms at {terms_path.resolve()}\n"
        f"Bounty: {summary.repo}#{summary.issue} — {terms.title}\n"
        f"Revision: {current.revision}  digest {current.spec_digest}\n"
        f"Entry fee: {alpha(terms.entry_fee_alpha_rao)}; reward pool "
        f"{alpha(terms.reward_pool_alpha_rao)}\n"
        f"Delivery: {terms.delivery_days} days from the fee; review target "
        f"{terms.review_days} days; unpaid holds lapse after "
        f"{terms.hold_minutes} minutes\n"
        f"Fee escrow: coldkey {terms.fee_escrow_coldkey}, hotkey "
        f"{terms.fee_escrow_hotkey}\n"
        "Funding: REHEARSAL. The fee is simulated and no funds move.\n"
        f"Claimant hotkey: {handle.hotkey_ss58}\nPayee coldkey: {payee}\n"
        f"GitHub login: {args.github_login}",
        file=sys.stderr,
    )
    confirmation = (
        f"CLAIM BOUNTY {summary.repo}#{summary.issue} REVISION {current.revision} "
        f"{current.spec_digest}"
    )
    value = (
        args.confirmation
        if args.confirmation is not None
        else prompt_input(f"Type exactly '{confirmation}' to claim: ")
    )
    if value != confirmation:
        raise MinerCliError(
            "the claim was not signed: the typed confirmation did not match this "
            "revision and digest"
        )
    try:
        key = wallet.hotkey if args.key_kind == "hotkey" else wallet.coldkey
        signer = key.ss58_address
    except Exception:
        raise MinerCliError(
            f"cannot load or unlock the {args.key_kind} signing key; check the "
            "wallet keyfile and passphrase"
        ) from None
    nonce, issued = uuid4(), datetime.now(UTC)
    fields = {
        "netuid": netuid,
        "repo": summary.repo,
        "issue": summary.issue,
        "revision": current.revision,
        "spec_digest": current.spec_digest,
        "hotkey": handle.hotkey_ss58,
        "payee_coldkey": payee,
        "github_login": args.github_login,
        "team_digest": bounty_team_digest(
            payee_coldkey=payee, members=[(handle.hotkey_ss58, args.github_login)]
        ),
        "nonce": nonce,
        "issued_at": issued,
    }
    try:
        signature = key.sign(
            bounty_claim_message(**fields, key_kind=args.key_kind, signer=signer)
        ).hex()
    except Exception:
        # Wallet SDK errors can echo keyfile details; keep them private.
        raise MinerCliError(
            "cannot sign the claim; check the signing key and retry"
        ) from None
    body = BountyClaimRequest(
        **fields,
        proof=ContributorProof(
            key_kind=args.key_kind, signer=signer, signature=signature
        ),
    )
    held = _retrying(lambda: client.hold_bounty_claim(body))
    _save(
        cache / "claims" / f"{held.claim_id}.json",
        {
            "claim": held.model_dump(mode="json"),
            "request": body.model_dump(mode="json"),
        },
    )
    print(
        f"Holding round {held.round} slot {held.position} until "
        f"{held.hold_expires_at.isoformat()} (claim {held.claim_id})",
        file=sys.stderr,
    )
    result = _confirm_fee(client, held)
    if args.json:
        _print_json(result)
    else:
        _print_claim(result)
    return 0 if result.state == "claimed" else 1


def network_netuid(client: ApiClient) -> int:
    """The deployment's netuid, as Platform reports it for signed actions."""
    status = client.get_cla_status()
    if status is None:
        raise MinerCliError("this Platform does not support contributor identity yet")
    return status.netuid


def run(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    try:
        with ApiClient(base_url=network.api_url) as client:
            command = args.bounty_command
            if command == "list":
                page = client.list_bounties(limit=100)
                if args.json:
                    _print_json(page)
                    return 0
                if not page.items:
                    print("no bounties are published")
                for item in page.items:
                    paused = "  [claims paused]" if item.claims_paused else ""
                    print(
                        f"{item.repo}#{item.issue}  r{item.current_revision}  "
                        f"fee {alpha(item.entry_fee_alpha_rao)}  open "
                        f"{item.slots.open}  held {item.slots.held}  claimed "
                        f"{item.slots.claimed}  {item.title}{paused}"
                    )
                return 0
            if command == "show":
                detail = _find(client, args.target)
                if args.json:
                    _print_json(detail)
                    return 0
                summary = detail.summary
                terms = detail.revisions[-1].terms
                print(
                    f"{summary.repo}#{summary.issue} — {summary.title}\n"
                    f"Revision {summary.current_revision}  digest "
                    f"{summary.spec_digest}  ({summary.funding_mode})\n"
                    f"Entry fee {alpha(terms.entry_fee_alpha_rao)}; pool "
                    f"{alpha(terms.reward_pool_alpha_rao)}; delivery "
                    f"{terms.delivery_days} days; reviewer @{terms.reviewer_login}"
                    + ("\nNew claims are paused." if summary.claims_paused else "")
                )
                for slot in detail.slots:
                    who = (
                        f"  {slot.claimant.github_login} ({slot.claimant.hotkey})"
                        if slot.claimant
                        else ""
                    )
                    print(
                        f"  round {slot.round} slot {slot.position}: {slot.status}"
                        + who
                    )
                return 0
            if command == "claim":
                return _claim(client, args, network.name)
            if command in ("submit", "withdraw", "appeal"):
                result = _act(client, args)
                if args.json:
                    _print_json(result)
                else:
                    _print_claim(result)
                return 0
            view = client.get_bounty_claim(args.claim_id)
            if command == "confirm":
                view = _confirm_fee(client, view)
            if args.json:
                _print_json(view)
            else:
                _print_claim(view)
            return 0
    except (MinerCliError, ValueError, EOFError, OSError) as exc:
        print(str(exc) or "bounty command cancelled", file=sys.stderr)
        return 1


def _consent(args: argparse.Namespace, phrase: str, action: str) -> None:
    value = (
        args.confirmation
        if args.confirmation is not None
        else prompt_input(f"Type exactly '{phrase}' to {action}: ")
    )
    if value != phrase:
        raise MinerCliError(
            f"nothing was signed: the typed confirmation did not match '{phrase}'"
        )


def _signed(args: argparse.Namespace, view: BountyClaimView) -> tuple[str, Any]:
    """The wallet key that may act on this claim, after checking it is ours."""
    names = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty(),
    )
    handle, wallet = load_wallet(coldkey_name=names[0], hotkey_name=names[1])
    if handle.hotkey_ss58 != view.hotkey:
        raise MinerCliError(
            f"claim {view.claim_id} belongs to hotkey {view.hotkey}, not this "
            f"wallet's {handle.hotkey_ss58}"
        )
    try:
        key = wallet.hotkey if args.key_kind == "hotkey" else wallet.coldkey
        signer = key.ss58_address
    except Exception:
        raise MinerCliError(
            f"cannot load or unlock the {args.key_kind} signing key; check the "
            "wallet keyfile and passphrase"
        ) from None
    if args.key_kind == "coldkey" and signer != view.payee_coldkey:
        raise MinerCliError(
            "a coldkey proof must come from the claim's payee of record "
            f"{view.payee_coldkey}"
        )
    return signer, key


def _signature(key: Any, payload: bytes) -> str:
    try:
        return key.sign(payload).hex()
    except Exception:
        # Wallet SDK errors can echo keyfile details; keep them private.
        raise MinerCliError("cannot sign; check the signing key and retry") from None


def _act(client: ApiClient, args: argparse.Namespace) -> BountyClaimView:
    """Sign and send one submit, withdraw or appeal action."""
    view = client.get_bounty_claim(args.claim_id)
    netuid = network_netuid(client)
    command, claim_id = args.bounty_command, view.claim_id
    reason = ""
    head_sha = ""
    if command == "submit":
        head_sha = args.head_sha.lower()
        phrase = f"SUBMIT BOUNTY CLAIM {claim_id} PR {args.pr_number} {head_sha}"
        print(
            f"Submitting {view.repo}#{args.pr_number} at commit {head_sha} for "
            "review. The reviewer decides on this exact commit; after a new push, "
            "submit again.",
            file=sys.stderr,
        )
    elif command == "withdraw":
        phrase = f"WITHDRAW BOUNTY CLAIM {claim_id}"
        print(
            f"Withdrawing gives up round {view.round} slot {view.position}. The "
            f"entry fee of {alpha(view.fee_alpha_rao)} is retained.",
            file=sys.stderr,
        )
    else:
        reason = args.reason_file.read_text(encoding="utf-8")
        if not reason.strip():
            raise MinerCliError("the appeal statement is empty")
        phrase = f"APPEAL BOUNTY EVENT {args.event}"
        print(
            f"Appealing event {args.event} of claim {claim_id}; the statement's "
            f"digest is {bounty_reason_digest(reason)}",
            file=sys.stderr,
        )
    _consent(args, phrase, command)
    signer, key = _signed(args, view)
    nonce, issued = uuid4(), datetime.now(UTC)
    common: dict[str, Any] = {
        "netuid": netuid,
        "claim_id": claim_id,
        "hotkey": view.hotkey,
        "nonce": nonce,
        "issued_at": issued,
    }

    def proof(payload: bytes) -> ContributorProof:
        return ContributorProof(
            key_kind=args.key_kind, signer=signer, signature=_signature(key, payload)
        )

    if command == "submit":
        submitted = BountySubmitRequest(
            **common,
            repo=view.repo,
            pr_number=args.pr_number,
            head_sha=head_sha,
            proof=proof(
                bounty_submit_message(
                    **common,
                    repo=view.repo,
                    pr_number=args.pr_number,
                    head_sha=head_sha,
                    key_kind=args.key_kind,
                    signer=signer,
                )
            ),
        )
        return _retrying(lambda: client.submit_bounty_work(claim_id, submitted))
    if command == "withdraw":
        withdrawn = BountyWithdrawRequest(
            **common,
            proof=proof(
                bounty_withdraw_message(**common, key_kind=args.key_kind, signer=signer)
            ),
        )
        return _retrying(lambda: client.withdraw_bounty_claim(claim_id, withdrawn))
    appealed = BountyAppealRequest(
        **common,
        subject_event_id=args.event,
        reason=reason,
        proof=proof(
            bounty_appeal_message(
                **common,
                subject_event_id=args.event,
                reason_digest=bounty_reason_digest(reason),
                key_kind=args.key_kind,
                signer=signer,
            )
        ),
    )
    return _retrying(lambda: client.appeal_bounty_decision(claim_id, appealed))
