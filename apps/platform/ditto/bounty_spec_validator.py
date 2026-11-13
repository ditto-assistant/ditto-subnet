"""Scoped build-bounty specification and governance validator (#2044).

Validates that a proposed build bounty specification satisfies the qualification,
disqualification, alpha sizing, round sizing, and governance role-separation
rules defined in ``docs/bounty-specification.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_SAFE_RAO = (1 << 53) - 1
DEFAULT_ATTEMPTS_PER_ROUND = 3
MAX_FEE_TO_POOL_RATIO = 0.25
REPO_ISSUE_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+#[1-9][0-9]*$")

DISQUALIFIED_KEYWORDS = (
    "open-ended research",
    "hot-wallet private key",
    "validator weight signing key",
    "production authority bypass",
    "subjective cosmetic polish",
)


@dataclass(frozen=True)
class BountySpecProposal:
    repo: str
    issue: int
    title: str
    scope: str
    public_interfaces: str
    acceptance_evidence: str
    reviewer_login: str
    reviewer_actor: str
    author_login: str
    entry_fee_alpha_rao: int
    reward_pool_alpha_rao: int
    attempts_per_round: int = DEFAULT_ATTEMPTS_PER_ROUND
    delivery_days: int = 5
    review_days: int = 7
    hold_minutes: int = 30
    funding_mode: str = "rehearsal"
    fee_escrow_coldkey: str = ""
    fee_escrow_hotkey: str = ""
    dependencies: list[str] = field(default_factory=list)


def validate_bounty_spec_proposal(proposal: BountySpecProposal) -> list[str]:
    """Returns a list of policy refusal reasons; empty list when valid."""
    refusals: list[str] = []

    for field_name in ("title", "scope", "public_interfaces", "acceptance_evidence"):
        val = getattr(proposal, field_name, "").strip()
        if not val:
            refusals.append(f"{field_name} must not be blank")

    combined_text = f"{proposal.title} {proposal.scope}".lower()
    for forbidden in DISQUALIFIED_KEYWORDS:
        if forbidden in combined_text:
            refusals.append(
                f"disqualified bounty scope: contains prohibited category '{forbidden}'"
            )

    if not proposal.reviewer_login.strip():
        refusals.append("reviewer_login must not be blank")
    if not proposal.reviewer_actor.strip():
        refusals.append("reviewer_actor must not be blank")

    if proposal.entry_fee_alpha_rao <= 0 or proposal.entry_fee_alpha_rao > MAX_SAFE_RAO:
        refusals.append("entry_fee_alpha_rao must be a positive integer <= 2^53 - 1")
    if (
        proposal.reward_pool_alpha_rao <= 0
        or proposal.reward_pool_alpha_rao > MAX_SAFE_RAO
    ):
        refusals.append("reward_pool_alpha_rao must be a positive integer <= 2^53 - 1")

    if (
        proposal.entry_fee_alpha_rao > 0
        and proposal.reward_pool_alpha_rao > 0
        and proposal.entry_fee_alpha_rao
        > proposal.reward_pool_alpha_rao * MAX_FEE_TO_POOL_RATIO
    ):
        refusals.append(
            "entry_fee_alpha_rao must not exceed 25% of reward_pool_alpha_rao"
        )

    if proposal.attempts_per_round != DEFAULT_ATTEMPTS_PER_ROUND:
        refusals.append(
            f"attempts_per_round must be {DEFAULT_ATTEMPTS_PER_ROUND} in v1"
        )

    if not (1 <= proposal.delivery_days <= 90):
        refusals.append("delivery_days must be between 1 and 90")
    if not (1 <= proposal.review_days <= 30):
        refusals.append("review_days must be between 1 and 30")
    if not (1 <= proposal.hold_minutes <= 1440):
        refusals.append("hold_minutes must be between 1 and 1440")

    if proposal.funding_mode != "rehearsal":
        refusals.append(
            "live funding needs separate operator approval; publish in rehearsal mode"
        )

    if (
        not proposal.fee_escrow_coldkey.strip()
        or not proposal.fee_escrow_hotkey.strip()
        or proposal.fee_escrow_coldkey == proposal.fee_escrow_hotkey
    ):
        refusals.append("fee_escrow_coldkey and fee_escrow_hotkey must be distinct non-empty keys")

    for dep in proposal.dependencies:
        if not REPO_ISSUE_RE.match(dep):
            refusals.append(f"invalid dependency reference '{dep}'; expected owner/repo#issue")

    return refusals


def check_governance_role_separation(
    *,
    claimant_github_login: str,
    reviewer_login: str,
    reviewer_actor: str,
    award_decider_actor: str | None = None,
    appeal_decider_actor: str | None = None,
    payout_approver_actors: list[str] | None = None,
) -> list[str]:
    """Checks governance separation-of-duties invariants across lifecycle roles."""
    violations: list[str] = []

    if claimant_github_login.strip().lower() == reviewer_login.strip().lower():
        violations.append("the bounty's reviewer cannot claim a slot on it")

    if appeal_decider_actor and appeal_decider_actor.strip() == reviewer_actor.strip():
        violations.append("an appeal is decided by someone other than its reviewer")

    if payout_approver_actors:
        if len(set(payout_approver_actors)) != len(payout_approver_actors):
            violations.append("payout approvals must come from distinct operators")
        for approver in payout_approver_actors:
            if approver == reviewer_actor or (
                award_decider_actor and approver == award_decider_actor
            ):
                violations.append(
                    "whoever decided this award or accepted this work cannot approve it"
                )

    return violations
