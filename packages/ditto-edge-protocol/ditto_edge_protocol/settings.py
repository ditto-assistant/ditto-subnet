"""Operator tunables for one edge lane, with the shadow defaults.

Platform stores these as revisioned settings; the in-memory coordinator takes
them directly. Defaults keep a shadow run cheap and slow: 3 replicas, 8 MiB
objects, 2 GiB per node, 6 challenge slots per hourly window.
"""

from __future__ import annotations

from dataclasses import dataclass

from ditto_edge_protocol.ledger import LedgerParams
from ditto_edge_protocol.limits import GIB, MAX_OBJECT_BYTES_CEILING, MIB
from ditto_edge_protocol.wire import LaneLimits, LaneMode


@dataclass(frozen=True)
class LaneSettings:
    mode: LaneMode = "off"
    max_nodes: int = 100
    replicas: int = 3
    max_object_bytes: int = 8 * MIB
    node_quota_cap_bytes: int = 2 * GIB
    lease_seconds: int = 900
    poll_interval_seconds: int = 30
    challenge_deadline_seconds: int = 60
    window_seconds: int = 3600
    challenges_per_window: int = 6
    retrieval_share: float = 0.2
    revoked_share: float = 0.15
    min_challenges: int = 3
    availability_floor: float = 0.8
    max_new_assignments_per_poll: int = 16
    revoked_lookback_seconds: int = 86_400

    def __post_init__(self) -> None:
        checks = (
            (self.mode in ("off", "shadow"), "mode must be off or shadow"),
            (1 <= self.max_nodes <= 10_000, "max_nodes must be 1..10000"),
            (1 <= self.replicas <= 16, "replicas must be 1..16"),
            (
                1 <= self.max_object_bytes <= MAX_OBJECT_BYTES_CEILING,
                "max_object_bytes exceeds the protocol ceiling",
            ),
            (self.node_quota_cap_bytes >= 0, "node_quota_cap_bytes must be >= 0"),
            (60 <= self.lease_seconds <= 86_400, "lease_seconds must be 60..86400"),
            (
                5 <= self.poll_interval_seconds <= 3600,
                "poll_interval_seconds must be 5..3600",
            ),
            (
                5 <= self.challenge_deadline_seconds <= 3600,
                "challenge_deadline_seconds must be 5..3600",
            ),
            (300 <= self.window_seconds <= 86_400, "window_seconds must be 300..86400"),
            (
                0 <= self.challenges_per_window <= 120,
                "challenges_per_window must be 0..120",
            ),
            (0.0 <= self.retrieval_share <= 1.0, "retrieval_share must be 0..1"),
            (0.0 <= self.revoked_share <= 1.0, "revoked_share must be 0..1"),
            (
                self.retrieval_share + self.revoked_share <= 1.0,
                "retrieval_share + revoked_share must be <= 1",
            ),
            (
                0 <= self.min_challenges <= self.challenges_per_window,
                "min_challenges too high",
            ),
            (0.0 <= self.availability_floor <= 1.0, "availability_floor must be 0..1"),
            (
                0 <= self.max_new_assignments_per_poll <= 256,
                "max_new_assignments_per_poll must be 0..256",
            ),
            (
                self.revoked_lookback_seconds >= 0,
                "revoked_lookback_seconds must be >= 0",
            ),
            (
                self.lease_seconds >= 2 * self.poll_interval_seconds,
                "lease_seconds must cover at least two polls",
            ),
        )
        for ok, message in checks:
            if not ok:
                raise ValueError(message)

    @property
    def grace_seconds(self) -> int:
        """How late a node may poll for a due slot and still be challenged."""
        return 2 * self.poll_interval_seconds + 5

    @property
    def late_slot_windows(self) -> int:
        """How many earlier windows can hold a slot still live within grace."""
        return -(-self.grace_seconds // self.window_seconds)

    def limits(self) -> LaneLimits:
        return LaneLimits(
            mode=self.mode,
            poll_interval_seconds=self.poll_interval_seconds,
            lease_seconds=self.lease_seconds,
            max_object_bytes=self.max_object_bytes,
            node_quota_cap_bytes=self.node_quota_cap_bytes,
            challenge_deadline_seconds=self.challenge_deadline_seconds,
        )

    def ledger_params(self) -> LedgerParams:
        return LedgerParams(
            min_challenges=self.min_challenges,
            availability_floor=self.availability_floor,
        )
