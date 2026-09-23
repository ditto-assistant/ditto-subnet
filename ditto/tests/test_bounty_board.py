"""Tests for SN118 bounty board definitions, queries, and lifecycle policies."""

from pathlib import Path

import pytest

from ditto.bounty.board import (
    ALL_BOUNTY_LABELS,
    COMPONENT_LABELS,
    LABELS_DO_NOT_AUTHORIZE_PAYMENT,
    STATUS_LABELS,
    BountyComponent,
    BountyStatus,
    build_board_query,
    build_component_query,
    build_stale_claims_query,
    generate_gh_label_commands,
    validate_state_transition,
)

REPO_ROOT = Path(__file__).parents[2]


def test_payment_authorization_invariant() -> None:
    """Labels must never authorize or trigger financial payments."""
    assert LABELS_DO_NOT_AUTHORIZE_PAYMENT is True


def test_status_labels_completeness() -> None:
    """Every defined status must have complete metadata."""
    for status in BountyStatus:
        assert status.value in STATUS_LABELS
        label = STATUS_LABELS[status.value]
        assert label.name == status.value
        assert len(label.color) == 6
        assert label.description


def test_component_labels_completeness() -> None:
    """Every defined subsystem component must have complete metadata."""
    for component in BountyComponent:
        assert component.value in COMPONENT_LABELS
        label = COMPONENT_LABELS[component.value]
        assert label.name == component.value
        assert len(label.color) == 6
        assert label.description


def test_all_bounty_labels_aggregation() -> None:
    """Aggregated label dict must include base tag, statuses, and components."""
    assert "bounty" in ALL_BOUNTY_LABELS
    for status in BountyStatus:
        assert status.value in ALL_BOUNTY_LABELS
    for component in BountyComponent:
        assert component.value in ALL_BOUNTY_LABELS


def test_board_queries_generation() -> None:
    """Search queries must be defined and distinct for each lifecycle state."""
    open_query = build_board_query(BountyStatus.OPEN)
    assert "is:open" in open_query
    assert "-label:status:claimed" in open_query
    assert "-label:status:in-review" in open_query

    claimed_query = build_board_query(BountyStatus.CLAIMED)
    assert "label:status:claimed" in claimed_query

    in_review_query = build_board_query(BountyStatus.IN_REVIEW)
    assert "label:status:in-review" in in_review_query

    accepted_query = build_board_query(BountyStatus.ACCEPTED)
    assert "label:status:accepted" in accepted_query

    paid_query = build_board_query(BountyStatus.PAID)
    assert "label:status:paid" in paid_query

    blocked_query = build_board_query(BountyStatus.BLOCKED)
    assert "label:status:blocked" in blocked_query

    cancelled_query = build_board_query(BountyStatus.CANCELLED)
    assert "is:closed" in cancelled_query
    assert "-label:status:paid" in cancelled_query


def test_board_query_invalid_status() -> None:
    """Invalid statuses must raise a ValueError."""
    with pytest.raises(ValueError, match="Unknown bounty status"):
        build_board_query("status:invalid")


def test_stale_claims_query() -> None:
    """Stale claims query must format the cutoff date parameter accurately."""
    cutoff = "2026-09-15"
    query = build_stale_claims_query(cutoff)
    assert "label:status:claimed" in query
    assert f"updated:<{cutoff}" in query


def test_component_query() -> None:
    """Component query must filter by the requested component tag."""
    query = build_component_query(BountyComponent.MINER)
    assert "label:component:miner" in query


def test_lifecycle_state_transitions() -> None:
    """State transitions must enforce gate ordering."""
    assert validate_state_transition(
        BountyStatus.OPEN.value, BountyStatus.CLAIMED.value
    )
    assert validate_state_transition(
        BountyStatus.CLAIMED.value, BountyStatus.IN_REVIEW.value
    )
    assert validate_state_transition(
        BountyStatus.IN_REVIEW.value, BountyStatus.ACCEPTED.value
    )
    assert validate_state_transition(
        BountyStatus.ACCEPTED.value, BountyStatus.PAID.value
    )

    assert not validate_state_transition(
        BountyStatus.OPEN.value, BountyStatus.PAID.value
    )
    assert not validate_state_transition(
        BountyStatus.IN_REVIEW.value, BountyStatus.PAID.value
    )
    assert not validate_state_transition(
        BountyStatus.PAID.value, BountyStatus.OPEN.value
    )


def test_generate_gh_label_commands() -> None:
    """Label commands must generate valid commands for each label."""
    commands = generate_gh_label_commands(repo="custom/repo")
    assert len(commands) == len(ALL_BOUNTY_LABELS)
    assert all("custom/repo" in cmd for cmd in commands)
    assert all("gh label create" in cmd for cmd in commands)


def test_issue_template_contents() -> None:
    """Bounty template must satisfy specifications and contain no emojis."""
    template_path = REPO_ROOT / ".github/ISSUE_TEMPLATE/bounty.md"
    assert template_path.is_file()

    content = template_path.read_text(encoding="utf-8")
    assert "SN118-BOUNTY-" in content
    assert "Spec Revision" in content
    assert "Reviewer / Technical Owner" in content
    assert "Reward Status" in content
    assert "Proposed / Unfunded" in content
    assert "Claim Expiry Window" in content
    assert "Scope & Deliverables" in content
    assert "Dependencies & Conflicts" in content
    assert "Acceptance Criteria & Evidence Verification" in content
    assert "docs/CONTRIBUTING_BOUNTIES.md" in content
    assert "Pull request merge does NOT authorize payment" in content
    assert "ditto-bounty-claim:v1" in content

    for char in content:
        assert ord(char) < 0x1F000


def test_contributor_guide_contents() -> None:
    """Contributor guide must define operational rules and contain no emojis."""
    guide_path = REPO_ROOT / "docs/CONTRIBUTING_BOUNTIES.md"
    assert guide_path.is_file()

    content = guide_path.read_text(encoding="utf-8")
    assert "SN118 5% Maintenance Treasury" in content
    assert "Pull request merge does NOT authorize payment" in content
    assert "ditto-bounty-claim:v1" in content
    assert "AGENTS.md" in content
    assert "CLAUDE.md" not in content
    assert "Balances.transfer_keep_alive" in content
    assert "ExtrinsicSuccess" in content
    assert "is:issue is:open label:bounty label:status:claimed" in content
    assert "updated:<YYYY-MM-DD" in content

    for char in content:
        assert ord(char) < 0x1F000
