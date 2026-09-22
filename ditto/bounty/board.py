"""Canonical board configuration, queries, and label definitions for SN118 bounties."""

from dataclasses import dataclass
from enum import StrEnum

LABELS_DO_NOT_AUTHORIZE_PAYMENT: bool = True


class BountyStatus(StrEnum):
    """Lifecycle status states for SN118 bounties."""

    OPEN = "status:open"
    CLAIMED = "status:claimed"
    IN_REVIEW = "status:in-review"
    ACCEPTED = "status:accepted"
    PAID = "status:paid"
    BLOCKED = "status:blocked"
    CANCELLED = "status:cancelled"


class BountyComponent(StrEnum):
    """Subnet 118 architectural component domains."""

    MINER = "component:miner"
    VALIDATOR = "component:validator"
    PLATFORM = "component:platform"
    BACKROOM = "component:backroom"
    DITTOBENCH = "component:dittobench"
    INFRA = "component:infra"
    SECURITY = "component:security"


@dataclass(frozen=True)
class BountyLabel:
    """Label metadata describing name, hex color, and descriptive purpose."""

    name: str
    color: str
    description: str


STATUS_LABELS: dict[str, BountyLabel] = {
    BountyStatus.OPEN.value: BountyLabel(
        name=BountyStatus.OPEN.value,
        color="0E8A16",
        description="Bounty is scoped, reviewed, and open for contributor claims",
    ),
    BountyStatus.CLAIMED.value: BountyLabel(
        name=BountyStatus.CLAIMED.value,
        color="FBCA04",
        description="Contributor holds an active reservation (7-day default)",
    ),
    BountyStatus.IN_REVIEW.value: BountyLabel(
        name=BountyStatus.IN_REVIEW.value,
        color="1D76DB",
        description="Pull request submitted and actively linked for review",
    ),
    BountyStatus.ACCEPTED.value: BountyLabel(
        name=BountyStatus.ACCEPTED.value,
        color="6F42C1",
        description="Deliverable verified and accepted by designated reviewers",
    ),
    BountyStatus.PAID.value: BountyLabel(
        name=BountyStatus.PAID.value,
        color="0075CA",
        description="Payout executed on-chain and reconciled against ledger",
    ),
    BountyStatus.BLOCKED.value: BountyLabel(
        name=BountyStatus.BLOCKED.value,
        color="E4E669",
        description="Progress halted due to upstream dependencies or governance holds",
    ),
    BountyStatus.CANCELLED.value: BountyLabel(
        name=BountyStatus.CANCELLED.value,
        color="CFD3D7",
        description="Bounty closed without payment, withdrawn, or rejected",
    ),
}

COMPONENT_LABELS: dict[str, BountyLabel] = {
    BountyComponent.MINER.value: BountyLabel(
        name=BountyComponent.MINER.value,
        color="0E8A16",
        description="SN118 miner reference harness, starter kits, and practice loops",
    ),
    BountyComponent.VALIDATOR.value: BountyLabel(
        name=BountyComponent.VALIDATOR.value,
        color="1D76DB",
        description="Validator worker, Pylon integration, and weight-setting pipeline",
    ),
    BountyComponent.PLATFORM.value: BountyLabel(
        name=BountyComponent.PLATFORM.value,
        color="5319E7",
        description="API server, models, dashboard, and database operations",
    ),
    BountyComponent.BACKROOM.value: BountyLabel(
        name=BountyComponent.BACKROOM.value,
        color="D93F0B",
        description="SN118 backroom console and MCP control plane",
    ),
    BountyComponent.DITTOBENCH.value: BountyLabel(
        name=BountyComponent.DITTOBENCH.value,
        color="16E2E2",
        description="Benchmarking harness, Go API scorer, and dataset generation",
    ),
    BountyComponent.INFRA.value: BountyLabel(
        name=BountyComponent.INFRA.value,
        color="21CEFF",
        description="Cloud deployment, Dockerfiles, and CI/CD pipelines",
    ),
    BountyComponent.SECURITY.value: BountyLabel(
        name=BountyComponent.SECURITY.value,
        color="B60205",
        description="Security hardening, threat mitigations, and crypto verification",
    ),
}

ALL_BOUNTY_LABELS: dict[str, BountyLabel] = {
    "bounty": BountyLabel(
        name="bounty",
        color="0E8A16",
        description="Treasury-funded maintenance and engineering tasks for SN118",
    ),
    **STATUS_LABELS,
    **COMPONENT_LABELS,
}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    BountyStatus.OPEN.value: {
        BountyStatus.CLAIMED.value,
        BountyStatus.BLOCKED.value,
        BountyStatus.CANCELLED.value,
    },
    BountyStatus.CLAIMED.value: {
        BountyStatus.IN_REVIEW.value,
        BountyStatus.OPEN.value,
        BountyStatus.BLOCKED.value,
        BountyStatus.CANCELLED.value,
    },
    BountyStatus.IN_REVIEW.value: {
        BountyStatus.ACCEPTED.value,
        BountyStatus.OPEN.value,
        BountyStatus.BLOCKED.value,
        BountyStatus.CANCELLED.value,
    },
    BountyStatus.ACCEPTED.value: {
        BountyStatus.PAID.value,
        BountyStatus.BLOCKED.value,
    },
    BountyStatus.BLOCKED.value: {
        BountyStatus.OPEN.value,
        BountyStatus.CLAIMED.value,
        BountyStatus.IN_REVIEW.value,
        BountyStatus.ACCEPTED.value,
        BountyStatus.CANCELLED.value,
    },
    BountyStatus.PAID.value: set(),
    BountyStatus.CANCELLED.value: set(),
}


def build_board_query(status: BountyStatus | str) -> str:
    """Generate canonical GitHub search query string for the specified board view."""
    status_str = status.value if isinstance(status, BountyStatus) else status
    if status_str == BountyStatus.OPEN.value:
        return (
            "is:issue is:open label:bounty "
            "-label:status:claimed -label:status:in-review"
        )
    if status_str == BountyStatus.CLAIMED.value:
        return "is:issue is:open label:bounty label:status:claimed"
    if status_str == BountyStatus.IN_REVIEW.value:
        return "is:issue is:open label:bounty label:status:in-review"
    if status_str == BountyStatus.ACCEPTED.value:
        return "is:issue label:bounty label:status:accepted"
    if status_str == BountyStatus.PAID.value:
        return "is:issue label:bounty label:status:paid"
    if status_str == BountyStatus.BLOCKED.value:
        return "is:issue is:open label:bounty label:status:blocked"
    if status_str == BountyStatus.CANCELLED.value:
        return "is:issue is:closed label:bounty -label:status:paid"
    msg = f"Unknown bounty status: {status_str}"
    raise ValueError(msg)


def build_stale_claims_query(cutoff_date: str) -> str:
    """Generate query to find claimed bounties without activity beyond TTL cutoff."""
    return f"is:issue is:open label:bounty label:status:claimed updated:<{cutoff_date}"


def build_component_query(component: BountyComponent | str) -> str:
    """Generate search query targeting a specific subsystem component."""
    comp_str = component.value if isinstance(component, BountyComponent) else component
    return f"is:issue is:open label:bounty label:{comp_str}"


def validate_state_transition(from_state: str, to_state: str) -> bool:
    """Verify whether a state transition conforms to governance rules."""
    allowed = _ALLOWED_TRANSITIONS.get(from_state, set())
    return to_state in allowed


def generate_gh_label_commands(
    repo: str = "ditto-assistant/ditto-subnet",
) -> list[str]:
    """Emit reproducible gh CLI commands to configure canonical labels on GitHub."""
    commands: list[str] = []
    for label in ALL_BOUNTY_LABELS.values():
        cmd = (
            f"gh label create {label.name!r} "
            f"--color {label.color!r} "
            f"--description {label.description!r} "
            f"--repo {repo!r} --force"
        )
        commands.append(cmd)
    return commands
