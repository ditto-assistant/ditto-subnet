"""A real node against the in-memory coordinator, every request signed."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator

import pytest

from ditto_edge_protocol import paths
from ditto_edge_protocol.coordinator import Coordinator, CoordinatorError, _Assignment
from ditto_edge_protocol.dryrun import SimClock
from ditto_edge_protocol.limits import GIB
from ditto_edge_protocol.node import EdgeNode, NodeConfig
from ditto_edge_protocol.settings import LaneSettings
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.sources import synthetic_objects
from ditto_edge_protocol.store import MemoryStore
from ditto_edge_protocol.transport import LocalTransport, TransportError
from ditto_edge_protocol.wire import AckRequest, AckResponse, ClientInfo, PollRequest

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

    def node(
        self, quota: int = GIB, clock: Callable[[], float] | None = None
    ) -> EdgeNode:
        key = DeviceKey.from_seed(self.rng.randbytes(32))
        node = EdgeNode(
            transport=LocalTransport(coordinator=self.coordinator, key=key),
            store=MemoryStore(),
            config=NodeConfig(
                public_key_hex=key.public_key_hex,
                quota_bytes=quota,
                client=ClientInfo(name="test", version="1"),
            ),
            clock=clock if clock is not None else self.clock,
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


def test_a_slot_due_just_before_a_window_edge_is_still_issued() -> None:
    lane = Lane(_settings(challenges_per_window=6, min_challenges=3))
    for obj in synthetic_objects(4, seed=3):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    node = lane.node()
    lane.run([node], 3600 - 30)  # the last poll lands 30 s before the edge
    window = int(lane.clock.now // 3600)
    late = lane.coordinator.slots[(node.node_id, window)][-1]
    late.state = "scheduled"
    late.challenge_id = None
    late.due_at = lane.clock.now + 10  # due 20 s before the window ends
    lane.clock.advance(45)  # the first poll of the next window, inside grace
    node.step()
    assert int(lane.clock.now // 3600) == window + 1
    assert late.state == "issued"


def test_a_slot_is_issued_when_grace_spans_more_than_one_window() -> None:
    settings = _settings(
        window_seconds=300,
        poll_interval_seconds=200,
        challenges_per_window=6,
        min_challenges=3,
    )
    assert settings.grace_seconds > settings.window_seconds
    assert settings.late_slot_windows == 2
    lane = Lane(settings)
    for obj in synthetic_objects(4, seed=3):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    node = lane.node()
    assert node.node_id is not None
    lane.run([node], 1200)
    node.step()
    window = int(lane.clock.now // 300)
    late = lane.coordinator.slots[(node.node_id, window)][-1]
    late.state = "scheduled"
    late.challenge_id = None
    late.due_at = (window + 1) * 300 - 5  # due 5 s before its window ends
    # The node skips the next window entirely and returns two windows on, still
    # inside grace: the slot is two windows old and must be issued.
    lane.clock.advance(late.due_at + settings.grace_seconds - 5 - lane.clock.now)
    node.step()
    assert int(lane.clock.now // 300) == window + 2
    assert late.state == "issued"


def _spans(lane: Lane, node: EdgeNode) -> dict[str, list[_Assignment]]:
    by_digest: dict[str, list[_Assignment]] = {}
    for a in [*lane.coordinator.history, *lane.coordinator.assignments.values()]:
        if a.node_id == node.node_id:
            by_digest.setdefault(a.digest, []).append(a)
    return by_digest


def test_a_lapsed_lease_closes_at_the_lapse_and_reopens_without_a_download() -> None:
    # One new placement per poll: reopening what a node still holds must not be
    # rationed like a download, or a node back from sleep would drop its cache.
    lane = Lane(_settings(replicas=1, max_new_assignments_per_poll=1))
    for obj in synthetic_objects(6, seed=3):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    node = lane.node()
    assert node.node_id is not None
    lane.run([node], 600)
    assert len(node.store.digests()) == 6
    lease_end = {
        a.digest: a.lease_expires_at
        for a in lane.coordinator.assignments.values()
        if a.node_id == node.node_id
    }
    # Asleep past the lease: Platform hears nothing until the node returns.
    lane.clock.advance(lane.coordinator.settings.lease_seconds + 600)
    returned_at = lane.clock.now
    report = node.step()
    assert report.filled == []
    assert len(node.store.digests()) == 6
    old_spans = {
        a.digest: a for a in lane.coordinator.history if a.node_id == node.node_id
    }
    assert old_spans.keys() == lease_end.keys()
    for digest, end in lease_end.items():
        old = old_spans[digest]
        new = lane.coordinator.assignments[(node.node_id, digest)]
        # The old span ends at the lapse and the new one starts at the fresh
        # acknowledgement, so nothing credits the time the node was away.
        assert old.released_at == end
        assert new.released_at is None
        assert new.acked_at is not None and new.acked_at > returned_at
        assert new.fill_count == old.fill_count


def test_a_node_with_a_slow_clock_still_reacknowledges_a_reopened_span() -> None:
    lane = Lane(_settings(replicas=1))
    for obj in synthetic_objects(4, seed=3):
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
    node = lane.node(clock=lambda: lane.clock() - 120)
    lane.run([node], 300)
    lease_end = max(
        a.lease_expires_at
        for a in lane.coordinator.assignments.values()
        if a.node_id == node.node_id
    )
    # Past the lease by Platform's clock, still before it by the node's.
    lane.clock.advance(lease_end + 10 - lane.clock.now)
    node.step()
    spans = [
        a for a in lane.coordinator.assignments.values() if a.node_id == node.node_id
    ]
    assert len(spans) == 4
    assert all(a.acked_at is not None and a.acked_at > lease_end for a in spans)


def test_a_span_replaced_after_release_still_counts_in_its_window(lane: Lane) -> None:
    node = lane.node()
    lane.run([node], 600)
    left_at = lane.clock.now
    node.leave("restart")
    lane.clock.advance(60)
    node.enroll()
    lane.run([node], 600)
    spans = _spans(lane, node)
    assert spans and all(len(each) == 2 for each in spans.values())
    lane.clock.advance(2 * 3600)
    lane.coordinator.close_windows()
    window_start = START
    window_end = START + 3600
    row = next(
        r
        for r in lane.coordinator.ledger
        if r.node_id == node.node_id and r.window_index == int(START // 3600)
    )
    held_seconds = {"earlier": 0.0, "later": 0.0}
    for each in spans.values():
        for a in each:
            assert a.acked_at is not None
            end = a.released_at if a.released_at is not None else a.lease_expires_at
            size = len(lane.coordinator.objects[a.digest].data)
            part = "earlier" if a.acked_at < left_at else "later"
            held_seconds[part] += size * (
                min(end, window_end) - max(a.acked_at, window_start)
            )
    expected = (held_seconds["earlier"] + held_seconds["later"]) / GIB / 3600
    assert held_seconds["earlier"] > 0 and held_seconds["later"] > 0
    assert row.score.held_gib_hours == pytest.approx(expected, abs=1e-5)


def test_acknowledgements_stay_within_the_item_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ditto_edge_protocol.node as node_module

    monkeypatch.setattr(node_module, "MAX_ACK_ITEMS", 3)
    lane = Lane(_settings(replicas=1))
    digests = sorted(
        lane.coordinator.ingest(obj.data, obj.media_type, obj.label)
        for obj in synthetic_objects(8, seed=3)
    )
    node = lane.node()
    sent: list[AckRequest] = []
    ack = node.transport.ack

    def record(request: AckRequest) -> AckResponse:
        sent.append(request)
        return ack(request)

    monkeypatch.setattr(node.transport, "ack", record)
    node.step()
    assert sorted(d for r in sent for d in r.held) == digests
    revoked = digests[:7]
    for digest in revoked:
        lane.coordinator.revoke(digest, "unpublished")
    lane.clock.advance(30)
    node.step()
    assert sorted(d for r in sent for d in r.released) == revoked
    assert len(sent) == 6
    assert all(len(r.held) <= 3 and len(r.released) <= 3 for r in sent)
    spans = {
        a.digest: a
        for a in lane.coordinator.assignments.values()
        if a.node_id == node.node_id
    }
    assert all(spans[d].acked_at is not None for d in digests)
    assert all(spans[d].released_at is not None for d in revoked)
