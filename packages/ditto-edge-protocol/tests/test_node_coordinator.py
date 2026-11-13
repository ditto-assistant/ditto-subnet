"""A real node against the in-memory coordinator, every request signed."""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from ditto_edge_protocol import paths
from ditto_edge_protocol.coordinator import Coordinator, CoordinatorError
from ditto_edge_protocol.dryrun import SimClock
from ditto_edge_protocol.limits import GIB
from ditto_edge_protocol.node import EdgeNode, NodeConfig
from ditto_edge_protocol.settings import LaneSettings
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.sources import synthetic_objects
from ditto_edge_protocol.store import MemoryStore
from ditto_edge_protocol.transport import LocalTransport, TransportError
from ditto_edge_protocol.wire import ClientInfo, PollRequest

START = 1_763_000_000.0 // 3600 * 3600


def _settings(**overrides: object) -> LaneSettings:
    base: dict[str, object] = {
        "mode": "shadow",
        "replicas": 2,
        "challenges_per_window": 12,
        "min_challenges": 6,
        "revoked_share": 0.25,
    }
    base.update(overrides)
    return LaneSettings(**base)  # type: ignore[arg-type]


class Lane:
    def __init__(self, settings: LaneSettings, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.clock = SimClock(START)
        self.coordinator = Coordinator(
            settings, clock=self.clock, rng=self.rng, nonce=self.rng.randbytes
        )

    def node(self, quota: int = GIB) -> EdgeNode:
        key = DeviceKey.from_seed(self.rng.randbytes(32))
        node = EdgeNode(
            transport=LocalTransport(coordinator=self.coordinator, key=key),
            store=MemoryStore(),
            config=NodeConfig(
                public_key_hex=key.public_key_hex,
                quota_bytes=quota,
                client=ClientInfo(name="test", version="1"),
            ),
            clock=self.clock,
        )
        node.enroll()
        return node

    def run(self, nodes: list[EdgeNode], seconds: int) -> None:
        for _ in range(seconds // self.coordinator.settings.poll_interval_seconds):
            for node in nodes:
                node.step()
            self.clock.advance(self.coordinator.settings.poll_interval_seconds)
            self.coordinator.close_windows()


@pytest.fixture
def lane() -> Iterator[Lane]:
    lane = Lane(_settings())
    for obj in synthetic_objects(12, seed=3):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    yield lane


def test_honest_nodes_fill_verify_ack_and_pass_every_challenge(lane: Lane) -> None:
    nodes = [lane.node() for _ in range(4)]
    lane.run(nodes, 3 * 3600)
    lane.clock.advance(3600 + 120)
    lane.coordinator.close_windows()
    rows = lane.coordinator.ledger
    assert rows and all(row.weight_eligible is False for row in rows)
    later = [row for row in rows if row.window_index > rows[0].window_index]
    assert later and all(row.score.eligible for row in later)
    assert all(row.score.failed == row.score.refilled == 0 for row in rows)
    # Every object has its replicas, and nodes hold exactly what was assigned.
    for digest in lane.coordinator.objects:
        holders = [
            a.node_id
            for a in lane.coordinator.assignments.values()
            if a.digest == digest and a.acked_at is not None and a.released_at is None
        ]
        assert len(holders) == 2


def test_revocation_deletes_and_later_challenges_are_refused(lane: Lane) -> None:
    nodes = [lane.node() for _ in range(3)]
    lane.run(nodes, 600)
    digest = next(iter(lane.coordinator.objects))
    holders = [n for n in nodes if n.store.has(digest)]
    assert holders
    lane.coordinator.revoke(digest, "unpublished")
    lane.run(nodes, 60)
    assert all(not n.store.has(digest) for n in nodes)
    assert all(
        a.released_at is not None
        for a in lane.coordinator.assignments.values()
        if a.digest == digest
    )
    lane.run(nodes, 3 * 3600)
    revoked = [
        c for c in lane.coordinator.challenges.values() if c.drawn.purpose == "revoked"
    ]
    assert revoked and all(c.outcome == "refused_ok" for c in revoked)


def test_lane_off_refuses_enrollment() -> None:
    lane = Lane(_settings(mode="off"))
    with pytest.raises(TransportError) as raised:
        lane.node()
    assert raised.value.status == 503


def test_replayed_or_foreign_requests_are_refused(lane: Lane) -> None:
    node = lane.node()
    transport = node.transport
    assert isinstance(transport, LocalTransport)
    body = b'{"free_bytes":0,"protocol":"edge/v1"}'
    signed = transport.key.sign_request(
        method="POST", path=paths.POLL, body=body, timestamp=int(lane.clock())
    ).as_dict()
    lane.coordinator.authenticate(
        method="POST", path=paths.POLL, headers=signed, body=body
    )
    with pytest.raises(CoordinatorError, match="replayed"):
        lane.coordinator.authenticate(
            method="POST", path=paths.POLL, headers=signed, body=body
        )
    stranger = DeviceKey.generate()
    forged = stranger.sign_request(
        method="POST", path=paths.POLL, body=body, timestamp=int(lane.clock())
    ).as_dict()
    with pytest.raises(CoordinatorError, match="unknown node"):
        lane.coordinator.authenticate(
            method="POST", path=paths.POLL, headers=forged, body=body
        )


def test_fill_requires_an_assignment(lane: Lane) -> None:
    node = lane.node()
    digest = next(iter(lane.coordinator.objects))
    with pytest.raises(TransportError) as raised:
        node.transport.fill(digest)
    assert raised.value.status == 404


def test_a_joining_node_takes_its_share_and_others_let_it_lapse(lane: Lane) -> None:
    first = [lane.node() for _ in range(2)]
    lane.run(first, 600)
    assert all(len(n.store.digests()) == 12 for n in first)
    late = [lane.node() for _ in range(2)]
    lane.run(first + late, 1800)
    assert all(n.store.digests() for n in late)
    assert all(len(n.store.digests()) < 12 for n in first)


def test_leaving_releases_everything_and_wipes_the_cache(lane: Lane) -> None:
    node = lane.node()
    lane.run([node], 300)
    assert node.store.digests()
    node.leave("done")
    assert node.store.digests() == set()
    assert all(
        a.released_at is not None
        for a in lane.coordinator.assignments.values()
        if a.node_id == node.node_id
    )
    with pytest.raises(TransportError) as raised:
        node.step()
    assert raised.value.status == 403


def test_status_reports_held_bytes_and_no_payouts(lane: Lane) -> None:
    node = lane.node()
    lane.run([node], 1200)
    status = node.transport.status()
    assert status.held_objects == len(node.store.digests())
    assert status.held_bytes == node.store.used_bytes()
    assert status.payouts == "none"
    assert status.current_window is not None


def test_live_status_does_not_count_a_pending_challenge_as_missed() -> None:
    lane = Lane(_settings(challenges_per_window=120, min_challenges=6))
    for obj in synthetic_objects(4, seed=3):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    node = lane.node()
    lane.run([node], 300)
    # Poll without answering: issue whatever is due, then read status at once.
    lane.clock.advance(60)
    response = node.transport.poll(PollRequest(protocol="edge/v1", free_bytes=GIB))
    assert response.challenges
    summary = node.transport.status().current_window
    assert summary is not None
    pending = len(response.challenges)
    decided = summary.passed + summary.missed + summary.failed
    assert summary.scheduled - decided >= pending
    # Past the deadline, the same unanswered challenges read as missed.
    lane.clock.advance(lane.coordinator.settings.challenge_deadline_seconds + 1)
    later = node.transport.status().current_window
    assert later is not None and later.missed >= pending


def test_a_poll_never_renews_more_than_it_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ditto_edge_protocol.coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "MAX_ASSIGNMENTS_PER_POLL", 3)
    lane = Lane(_settings(replicas=1, max_new_assignments_per_poll=16))
    for obj in synthetic_objects(8, seed=6):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    node = lane.node()
    response = node.transport.poll(PollRequest(protocol="edge/v1", free_bytes=GIB))
    assert len(response.assignments) == 3
    open_spans = [
        a
        for a in lane.coordinator.assignments.values()
        if a.node_id == node.node_id and a.released_at is None
    ]
    assert len(open_spans) == 3
