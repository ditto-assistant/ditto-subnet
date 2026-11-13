"""Unit tests for scoped build-bounty specification and governance rules (#2044)."""

from __future__ import annotations

import importlib.util
import os
import sys

SPEC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "bounty_spec_validator.py",
)
spec = importlib.util.spec_from_file_location("bounty_spec_validator", SPEC_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

BountySpecProposal = mod.BountySpecProposal
validate_bounty_spec_proposal = mod.validate_bounty_spec_proposal
check_governance_role_separation = mod.check_governance_role_separation


def _valid_proposal(**overrides) -> BountySpecProposal:
    defaults = {
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2044,
        "title": "Specify scoped build-bounty rules, alpha funding and governance",
        "scope": "docs/bounty-specification.md and apps/platform/ditto/bounty_spec_validator.py",
        "public_interfaces": "BountySpecProposal, validate_bounty_spec_proposal",
        "acceptance_evidence": "uv run pytest apps/platform/ditto/tests/test_bounty_spec_validator.py",
        "reviewer_login": "ditto-reviewer",
        "reviewer_actor": "reviewer@ditto.ai",
        "author_login": "ditto-author",
        "entry_fee_alpha_rao": 2_000_000_000,
        "reward_pool_alpha_rao": 100_000_000_000,
        "attempts_per_round": 3,
        "delivery_days": 5,
        "review_days": 7,
        "hold_minutes": 30,
        "funding_mode": "rehearsal",
        "fee_escrow_coldkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "fee_escrow_hotkey": "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        "dependencies": ["ditto-assistant/ditto-subnet#2045"],
    }
    defaults.update(overrides)
    return BountySpecProposal(**defaults)


def test_valid_bounty_spec_passes():
    proposal = _valid_proposal()
    assert validate_bounty_spec_proposal(proposal) == []


def test_disqualified_scope_and_excessive_fee_are_refused():
    proposal = _valid_proposal(
        scope="Open-ended research into hot-wallet private key rotation",
        entry_fee_alpha_rao=50_000_000_000,
        reward_pool_alpha_rao=100_000_000_000,
        funding_mode="live",
    )
    refusals = validate_bounty_spec_proposal(proposal)
    assert any("open-ended research" in r for r in refusals)
    assert any("25%" in r for r in refusals)
    assert any("rehearsal mode" in r for r in refusals)


def test_governance_role_separation_enforces_recusal_and_distinct_approvers():
    violations = check_governance_role_separation(
        claimant_github_login="Ditto-Reviewer",
        reviewer_login="ditto-reviewer",
        reviewer_actor="reviewer@ditto.ai",
        award_decider_actor="awarder@ditto.ai",
        appeal_decider_actor="reviewer@ditto.ai",
        payout_approver_actors=["awarder@ditto.ai", "awarder@ditto.ai"],
    )
    assert "the bounty's reviewer cannot claim a slot on it" in violations
    assert "an appeal is decided by someone other than its reviewer" in violations
    assert "payout approvals must come from distinct operators" in violations
    assert "whoever decided this award or accepted this work cannot approve it" in violations
