"""End-to-end tests for the Ditto Edge shadow lane on Platform (SN-481).

Signed edge/v1 requests go through the ASGI app to real Postgres. Only object
storage is an in-memory fake, and the lane clock is controlled so tests can
cross ledger windows.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.edge_lane import ObjectBytesCache
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.db.models import EdgeAssignment, EdgeChallengeSlot, EdgeLedgerWindow
from ditto_edge_protocol import paths
from ditto_edge_protocol.limits import GIB
from ditto_edge_protocol.proofs import digest_of, possession_proof
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.wire import (
    AckRequest,
    ChallengeAnswer,
    ClientInfo,
    EnrollRequest,
    PollRequest,
    PollResponse,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
START = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
WINDOW = 300
POLL = 30


class Clock:
    """Lane time that only moves forward, and never repeats a reading."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(microseconds=1)
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, *, key: str, body: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = body

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        if key not in self.objects:
            raise ObjectDownloadFailedError(key)
        return self.objects[key][:max_bytes]


@dataclass
class Lane:
    app: FastAPI
    client: httpx.AsyncClient
    clock: Clock
    storage: FakeStorage
    session_maker: async_sessionmaker[AsyncSession]

    async def settings(self, **overrides: object) -> httpx.Response:
        current = (
            await self.client.get("/api/v1/admin/edge-lane-settings", headers=_ADMIN)
        ).json()
        payload = {
            "expected_revision": current["effective"]["revision"],
            "scope": "*",
            "settings": {
                "mode": "shadow",
                "window_seconds": WINDOW,
                "poll_interval_seconds": POLL,
                "lease_seconds": 120,
                "challenge_deadline_seconds": 20,
                "challenges_per_window": 6,
                "min_challenges": 3,
                "replicas": 2,
                **overrides,
            },
            "reason": "SN-481 shadow lane end-to-end test",
            "actor": "test",
            "confirmation": "APPLY EDGE LANE SETTINGS",
        }
        return await self.client.post(
            "/api/v1/admin/edge-lane-settings", headers=_ADMIN, json=payload
        )

    async def ingest(self, data: bytes, media_type: str = "text/markdown") -> str:
        response = await self.client.post(
            "/api/v1/admin/edge/objects",
            headers=_ADMIN,
            json={
                "data_b64": base64.b64encode(data).decode(),
                "media_type": media_type,
                "source": "synthetic:test",
                "actor": "test",
            },
        )
        assert response.status_code == 200, response.text
        return str(response.json()["digest"])

    async def run(self, nodes: list[Node], seconds: int) -> None:
        for _ in range(seconds // POLL):
            for node in nodes:
                await node.step()
            self.clock.advance(POLL)

    async def ledger(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/v1/admin/edge/ledger", headers=_ADMIN)
        assert response.status_code == 200, response.text
        return list(response.json()["rows"])


@pytest.fixture
async def lane(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Lane]:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = session_maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    storage = FakeStorage()
    clock = Clock(START)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_storage_client] = lambda: storage
    app.state.edge_clock = clock
    app.state.edge_object_cache = ObjectBytesCache()
    app.state.edge_lane_settings.invalidate()
    yield Lane(app, client, clock, storage, session_maker)


@dataclass
class Node:
    """A signing node, following the reference node's step order."""

    lane: Lane
    behavior: str = "honest"
    key: DeviceKey = field(default_factory=DeviceKey.generate)
    store: dict[str, bytes] = field(default_factory=dict)
    leases: dict[str, int] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    acked: set[str] = field(default_factory=set)

    async def send(self, method: str, path: str, body: bytes = b"") -> httpx.Response:
        signed = self.key.sign_request(
            method=method,
            path=path,
            body=body,
            timestamp=int(self.lane.clock().timestamp()),
        )
        headers = signed.as_dict()
        if method == "POST":
            headers["Content-Type"] = "application/json"
        return await self.lane.client.request(
            method, path, content=body if method == "POST" else None, headers=headers
        )

    async def enroll(self) -> httpx.Response:
        body = EnrollRequest(
            protocol="edge/v1",
            public_key_hex=self.key.public_key_hex,
            quota_bytes=GIB,
            client=ClientInfo(name="test", version=self.behavior),
        ).model_dump_json(exclude_none=True)
        return await self.send("POST", paths.ENROLL, body.encode())

    async def fill(self, digest: str) -> bytes | None:
        response = await self.send("GET", paths.object_path(digest))
        return response.content if response.status_code == 200 else None

    async def step(self) -> PollResponse:
        body = PollRequest(protocol="edge/v1", free_bytes=GIB).model_dump_json()
        response = await self.send("POST", paths.POLL, body.encode())
        assert response.status_code == 200, response.text
        poll = PollResponse.model_validate_json(response.content)
        released: list[str] = []
        for digest in poll.revocations:
            released.append(digest)
            if self.behavior != "keeps_revoked":
                self.store.pop(digest, None)
                self.revoked.add(digest)
        for challenge in poll.challenges:
            data = self.store.get(challenge.digest)
            if challenge.digest in self.revoked:
                answer = ChallengeAnswer(outcome="refused", refusal="revoked")
            elif self.behavior == "lazy":
                data = await self.fill(challenge.digest)
                answer = self._answer(challenge, data)
            else:
                answer = self._answer(challenge, data)
            await self.send(
                "POST",
                paths.answer_path(challenge.challenge_id),
                answer.model_dump_json(exclude_none=True).encode(),
            )
        held: list[str] = []
        for assignment in poll.assignments:
            self.leases[assignment.digest] = assignment.lease_expires_at
            if assignment.digest in self.store or assignment.digest in self.revoked:
                continue
            data = await self.fill(assignment.digest)
            if data is not None and digest_of(data) == assignment.digest:
                if self.behavior != "lazy":
                    self.store[assignment.digest] = data
                held.append(assignment.digest)
        held += [
            d
            for d in (a.digest for a in poll.assignments)
            if d in self.store and d not in self.acked and d not in held
        ]
        if held or released:
            ack = AckRequest(held=sorted(set(held)), released=sorted(set(released)))
            await self.send("POST", paths.ACK, ack.model_dump_json().encode())
            self.acked.update(held)
        return poll

    def _answer(self, challenge, data: bytes | None) -> ChallengeAnswer:  # type: ignore[no-untyped-def]
        if data is None:
            return ChallengeAnswer(outcome="refused", refusal="not_held")
        if challenge.kind == "retrieval":
            window = data[challenge.offset : challenge.offset + challenge.length]
            if self.behavior == "tamper":
                window = bytes([window[0] ^ 1]) + window[1:]
            return ChallengeAnswer(
                outcome="bytes", data_b64=base64.b64encode(window).decode()
            )
        proof = possession_proof(
            nonce_hex=challenge.nonce_hex,
            data=data,
            offset=challenge.offset,
            length=challenge.length,
        )
        if self.behavior == "tamper":
            proof = ("1" if proof[0] == "0" else "0") + proof[1:]
        return ChallengeAnswer(outcome="proof", proof_hex=proof)


def _blob(index: int, size: int = 4096) -> bytes:
    seed = f"edge-lane-object-{index}".encode()
    return (seed * (size // len(seed) + 1))[:size]


async def _slots(lane: Lane, node: Node) -> list[EdgeChallengeSlot]:
    async with lane.session_maker() as session:
        return list(
            await session.scalars(
                select(EdgeChallengeSlot).where(
                    EdgeChallengeSlot.node_id == node.key.node_id
                )
            )
        )


async def test_the_lane_is_off_by_default(lane: Lane) -> None:
    response = await Node(lane).enroll()
    assert response.status_code == 503
    assert response.json()["error_code"] == 6001


async def test_honest_nodes_hold_answer_and_earn_shadow_units(lane: Lane) -> None:
    assert (await lane.settings()).status_code == 200
    for index in range(6):
        await lane.ingest(_blob(index))
    nodes = [Node(lane) for _ in range(3)]
    for node in nodes:
        assert (await node.enroll()).status_code == 200
    # Three full windows plus a poll past the last edge, as live nodes would.
    await lane.run(nodes, 3 * WINDOW + POLL)
    lane.clock.advance(WINDOW + 120)
    rows = await lane.ledger()
    assert rows
    assert all(row["weight_eligible"] is False for row in rows)
    windows = sorted({int(row["window_index"]) for row in rows})
    # The first window holds enrollment and the last ends with the run.
    middle = [row for row in rows if windows[0] < row["window_index"] < windows[-1]]
    assert middle and all(row["eligible"] for row in middle), middle
    assert all(row["failed"] == row["refilled"] == 0 for row in rows)
    for window in windows:
        shares = [
            row["hypothetical_share"] for row in rows if row["window_index"] == window
        ]
        assert sum(shares) == pytest.approx(1.0) or sum(shares) == 0.0
    async with lane.session_maker() as session:
        stored = list(await session.scalars(select(EdgeLedgerWindow)))
    assert stored and all(row.weight_eligible is False for row in stored)
    # Each object is held by its two rendezvous holders, no more.
    async with lane.session_maker() as session:
        spans = list(
            await session.scalars(
                select(EdgeAssignment).where(
                    EdgeAssignment.released_at.is_(None),
                    EdgeAssignment.acked_at.is_not(None),
                )
            )
        )
    by_digest: dict[str, int] = {}
    for span in spans:
        by_digest[span.digest] = by_digest.get(span.digest, 0) + 1
    assert set(by_digest.values()) == {2}


async def test_revocation_reaches_holders_and_checks_their_compliance(
    lane: Lane,
) -> None:
    await lane.settings(challenges_per_window=12, revoked_share=0.5, min_challenges=3)
    digests = [await lane.ingest(_blob(index)) for index in range(4)]
    honest, keeper = Node(lane), Node(lane, behavior="keeps_revoked")
    for node in (honest, keeper):
        await node.enroll()
    await lane.run([honest, keeper], 120)
    target = digests[0]
    revoke = await lane.client.post(
        f"/api/v1/admin/edge/objects/{target}/revoke",
        headers=_ADMIN,
        json={"reason": "the owner unpublished the share", "actor": "test"},
    )
    assert revoke.status_code == 200 and revoke.json()["newly_revoked"] is True
    poll = await honest.step()
    assert target in poll.revocations or target not in honest.store
    assert target not in honest.store
    await lane.run([honest, keeper], 2 * WINDOW)
    honest_checks = [s for s in await _slots(lane, honest) if s.purpose == "revoked"]
    keeper_checks = [s for s in await _slots(lane, keeper) if s.purpose == "revoked"]
    assert all(s.outcome in ("refused_ok", "pending") for s in honest_checks)
    assert any(s.outcome == "violation" for s in keeper_checks)
    async with lane.session_maker() as session:
        open_spans = list(
            await session.scalars(
                select(EdgeAssignment).where(
                    EdgeAssignment.digest == target,
                    EdgeAssignment.released_at.is_(None),
                )
            )
        )
    assert open_spans == []


async def test_a_lazy_node_is_caught_refilling_and_a_tamperer_fails(lane: Lane) -> None:
    await lane.settings(challenges_per_window=12, min_challenges=3, replicas=3)
    for index in range(4):
        await lane.ingest(_blob(index))
    lazy, tamper, honest = (
        Node(lane, behavior="lazy"),
        Node(lane, behavior="tamper"),
        Node(lane),
    )
    for node in (lazy, tamper, honest):
        await node.enroll()
    await lane.run([lazy, tamper, honest], 2 * WINDOW)
    lane.clock.advance(WINDOW + 120)
    rows = {(row["node_id"], row["window_index"]): row for row in await lane.ledger()}
    lazy_rows = [r for (n, _), r in rows.items() if n == lazy.key.node_id]
    tamper_rows = [r for (n, _), r in rows.items() if n == tamper.key.node_id]
    assert lazy_rows and sum(int(r["refilled"]) for r in lazy_rows) > 0
    assert not any(r["eligible"] for r in lazy_rows)
    assert tamper_rows and any("integrity_failure" in r["reasons"] for r in tamper_rows)
    assert not any(r["eligible"] for r in tamper_rows)


async def test_replayed_and_forged_requests_are_refused(lane: Lane) -> None:
    await lane.settings()
    node = Node(lane)
    await node.enroll()
    body = PollRequest(protocol="edge/v1", free_bytes=0).model_dump_json().encode()
    signed = node.key.sign_request(
        method="POST",
        path=paths.POLL,
        body=body,
        timestamp=int(lane.clock().timestamp()),
    ).as_dict()
    first = await lane.client.post(paths.POLL, content=body, headers=signed)
    assert first.status_code == 200
    replay = await lane.client.post(paths.POLL, content=body, headers=signed)
    assert replay.status_code == 401 and replay.json()["error_code"] == 6000
    stranger = Node(lane)
    forged = await stranger.send("POST", paths.POLL, body)
    assert forged.status_code == 401
    tampered = dict(signed, **{"X-Ditto-Edge-Nonce": "ab" * 16})
    assert (
        await lane.client.post(paths.POLL, content=body, headers=tampered)
    ).status_code == 401


async def test_fills_need_an_assignment(lane: Lane) -> None:
    await lane.settings()
    digest = await lane.ingest(_blob(1))
    node = Node(lane)
    await node.enroll()
    response = await node.send("GET", paths.object_path(digest))
    assert response.status_code == 404 and response.json()["error_code"] == 6004


async def test_admin_ingest_guards_and_reads(lane: Lane) -> None:
    await lane.settings(max_object_bytes=8192)
    bad_type = await lane.client.post(
        "/api/v1/admin/edge/objects",
        headers=_ADMIN,
        json={"data_b64": "aGVsbG8=", "media_type": "application/x-sh", "source": "x"},
    )
    assert bad_type.status_code == 422
    bad_b64 = await lane.client.post(
        "/api/v1/admin/edge/objects",
        headers=_ADMIN,
        json={"data_b64": "not base64!", "media_type": "text/plain", "source": "x"},
    )
    assert bad_b64.status_code == 422
    too_big = await lane.client.post(
        "/api/v1/admin/edge/objects",
        headers=_ADMIN,
        json={
            "data_b64": base64.b64encode(b"x" * 9000).decode(),
            "media_type": "text/plain",
            "source": "x",
        },
    )
    assert too_big.status_code == 413
    digest = await lane.ingest(_blob(7))
    again = await lane.client.post(
        "/api/v1/admin/edge/objects",
        headers=_ADMIN,
        json={
            "data_b64": base64.b64encode(_blob(7)).decode(),
            "media_type": "text/markdown",
            "source": "x",
        },
    )
    assert again.status_code == 200 and again.json()["created"] is False
    await lane.client.post(
        f"/api/v1/admin/edge/objects/{digest}/revoke",
        headers=_ADMIN,
        json={"reason": "test revocation reason", "actor": "test"},
    )
    revived = await lane.client.post(
        "/api/v1/admin/edge/objects",
        headers=_ADMIN,
        json={
            "data_b64": base64.b64encode(_blob(7)).decode(),
            "media_type": "text/markdown",
            "source": "x",
        },
    )
    assert revived.status_code == 409
    node = Node(lane)
    await node.enroll()
    await node.step()
    overview = (
        await lane.client.get("/api/v1/admin/edge/overview", headers=_ADMIN)
    ).json()
    assert overview["mode"] == "shadow"
    assert overview["weight_eligible"] is False and overview["payouts"] == "none"
    assert overview["nodes_active"] == 1 and overview["objects_revoked"] == 1
    listed = (await lane.client.get("/api/v1/admin/edge/nodes", headers=_ADMIN)).json()
    assert [n["node_id"] for n in listed["nodes"]] == [node.key.node_id]
    objects = (
        await lane.client.get("/api/v1/admin/edge/objects", headers=_ADMIN)
    ).json()
    assert {o["digest"] for o in objects["objects"]} == {digest}
    for path in ("overview", "nodes", "objects", "ledger"):
        assert (await lane.client.get(f"/api/v1/admin/edge/{path}")).status_code in (
            401,
            403,
        )


async def test_oversized_node_bodies_are_refused_before_reading(lane: Lane) -> None:
    await lane.settings()
    response = await lane.client.post(
        paths.POLL,
        content=b"{" + b" " * (70 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


async def test_status_reports_held_bytes_and_no_payouts(lane: Lane) -> None:
    await lane.settings()
    await lane.ingest(_blob(1))
    node = Node(lane)
    await node.enroll()
    await lane.run([node], 90)
    status = await node.send("GET", paths.STATUS)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["held_objects"] == 1
    assert body["held_bytes"] == len(_blob(1))
    assert body["payouts"] == "none"
    leave = await node.send("POST", paths.LEAVE, b"{}")
    assert leave.status_code == 204
    after = await node.send(
        "POST",
        paths.POLL,
        PollRequest(protocol="edge/v1", free_bytes=0).model_dump_json().encode(),
    )
    assert after.status_code == 403 and after.json()["error_code"] == 6003
