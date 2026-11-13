"""The Ditto Edge shadow lane on Platform (SN-481).

This module implements the ``edge/v1`` server rules over Postgres. It is the
same lane the in-memory coordinator in ``ditto_edge_protocol.coordinator``
runs for dry runs, and every decision goes through the shared pure functions,
so the two cannot disagree about what a node earned:

* :func:`~ditto_edge_protocol.placement.desired_objects` for placement;
* :func:`~ditto_edge_protocol.challenges.schedule_window`, ``draw_challenge``
  and ``judge_answer`` for challenges;
* :func:`~ditto_edge_protocol.ledger.score_window` and
  ``hypothetical_shares`` for the ledger.

Concurrency:

* A poll locks its node row, so one node's polls run one at a time.
* New nodes are admitted under a transaction lock, so the lane never takes
  more than ``max_nodes``.
* A request nonce is consumed by an atomic insert.
* A window is scored by whichever caller first inserts its closure row,
  inside the transaction that writes its ledger rows.

Shadow only. Ledger rows are written ``weight_eligible = false``, which a
check constraint also enforces. Nothing here reads or writes weights,
emissions or track shares.
"""

from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import Random, SystemRandom
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from ditto.api_models.edge_lane import EdgeLaneSettings
from ditto.api_server.attestation import verify_signature
from ditto.db.models import (
    EdgeAssignment,
    EdgeChallengeSlot,
    EdgeLaneState,
    EdgeLedgerWindow,
    EdgeNode,
    EdgeObject,
    EdgeRequestNonce,
    EdgeWindowClosure,
)
from ditto_edge_protocol.challenges import (
    DrawnChallenge,
    Held,
    Outcome,
    draw_challenge,
    judge_answer,
    schedule_window,
)
from ditto_edge_protocol.ledger import (
    HoldInterval,
    Observation,
    WindowScore,
    hypothetical_shares,
    score_window,
)
from ditto_edge_protocol.limits import (
    MAX_ASSIGNMENTS_PER_POLL,
    MAX_CHALLENGES_PER_POLL,
)
from ditto_edge_protocol.placement import (
    PlacementNode,
    PlacementObject,
    desired_objects,
)
from ditto_edge_protocol.signing import (
    HEADER_NODE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SignatureError,
    binding_message,
    node_id_for,
    verify_request,
)
from ditto_edge_protocol.wire import (
    AckRequest,
    AckResponse,
    Assignment,
    Challenge,
    ChallengeAnswer,
    EnrollRequest,
    EnrollResponse,
    NodeStatusResponse,
    PollRequest,
    PollResponse,
    WindowSummary,
)

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

BINDING_MAX_AGE = timedelta(days=30)
NONCE_RETENTION = timedelta(minutes=10)
MAX_WINDOWS_PER_CLOSE = 24
OBJECT_KEY_PREFIX = "edge-objects/"
ADMISSION_LOCK_KEY = "ditto:edge-lane-admission:v1"


class EdgeLaneError(Exception):
    """A request the lane refuses, mapped to an envelope status and code."""

    def __init__(self, status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# The 6xxx range; ditto/api_server/middleware/error_envelope.py re-exports these.
ERROR_CODE_EDGE_UNAUTHORIZED = 6000
ERROR_CODE_EDGE_LANE_OFF = 6001
ERROR_CODE_EDGE_LANE_FULL = 6002
ERROR_CODE_EDGE_NODE_INACTIVE = 6003
ERROR_CODE_EDGE_NOT_ASSIGNED = 6004
ERROR_CODE_EDGE_REVOKED = 6005
ERROR_CODE_EDGE_UNKNOWN_CHALLENGE = 6006
ERROR_CODE_EDGE_BAD_REQUEST = 6007
ERROR_CODE_EDGE_STORAGE_UNAVAILABLE = 6008


def _unauthorized(message: str) -> EdgeLaneError:
    return EdgeLaneError(401, ERROR_CODE_EDGE_UNAUTHORIZED, message)


def _ts(value: datetime) -> float:
    return value.timestamp()


def _dt(value: float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def object_key(digest: str) -> str:
    return f"{OBJECT_KEY_PREFIX}{digest}"


def edge_now(request: Request) -> datetime:
    """The lane's clock: real UTC time unless a test installed one."""
    clock = getattr(request.app.state, "edge_clock", None)
    return clock() if clock is not None else datetime.now(UTC)


class ObjectBytesCache:
    """Content-addressed bytes are immutable, so a cache is always correct."""

    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max = max_bytes
        self._used = 0
        self._entries: OrderedDict[str, bytes] = OrderedDict()

    def get(self, digest: str) -> bytes | None:
        data = self._entries.get(digest)
        if data is not None:
            self._entries.move_to_end(digest)
        return data

    def put(self, digest: str, data: bytes) -> None:
        if digest in self._entries or len(data) > self._max:
            return
        self._entries[digest] = data
        self._used += len(data)
        while self._used > self._max:
            _, evicted = self._entries.popitem(last=False)
            self._used -= len(evicted)


# -- authentication --------------------------------------------------------


@dataclass(frozen=True)
class Authenticated:
    node_id: str
    node: EdgeNode | None
    """``None`` only for an enrollment by a key Platform has not seen."""


async def authenticate(
    session: AsyncSession,
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
) -> Authenticated:
    """Verify one signed request and consume its nonce, or raise 401.

    Enrollment is self-certifying: the key travels in the body and must hash
    to the claimed node id. Every other request needs an enrolled node.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    node_id = lowered.get(HEADER_NODE.lower(), "")
    nonce = lowered.get(HEADER_NONCE.lower(), "")
    signature = lowered.get(HEADER_SIGNATURE.lower(), "")
    try:
        timestamp = int(lowered.get(HEADER_TIMESTAMP.lower(), ""))
    except ValueError as exc:
        raise _unauthorized("missing or malformed timestamp") from exc

    node = await session.get(EdgeNode, node_id) if node_id else None
    if node is not None:
        public_key = node.public_key_hex
    else:
        claimed: object = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                claimed = parsed.get("public_key_hex")
        except ValueError:
            pass
        if not isinstance(claimed, str):
            raise _unauthorized("unknown node")
        public_key = claimed
        try:
            if node_id_for(public_key) != node_id:
                raise _unauthorized("node id does not match its key")
        except SignatureError as exc:
            raise _unauthorized("malformed public key") from exc
    try:
        verify_request(
            public_key_hex=public_key,
            signature_hex=signature,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce_hex=nonce,
            body=body,
            now=int(_ts(now)),
        )
    except SignatureError as exc:
        raise _unauthorized(str(exc)) from exc
    inserted = await session.scalar(
        insert(EdgeRequestNonce)
        .values(node_id=node_id, nonce=nonce, seen_at=now)
        .on_conflict_do_nothing()
        .returning(EdgeRequestNonce.nonce)
    )
    if inserted is None:
        raise _unauthorized("nonce replayed")
    # Every signed request, not only polls, keeps the nonce table bounded.
    await prune_nonces(session, now=now)
    return Authenticated(node_id=node_id, node=node)


async def prune_nonces(session: AsyncSession, *, now: datetime) -> None:
    await session.execute(
        delete(EdgeRequestNonce).where(EdgeRequestNonce.seen_at < now - NONCE_RETENTION)
    )


def require_shadow(settings: EdgeLaneSettings) -> None:
    if settings.mode != "shadow":
        raise EdgeLaneError(503, ERROR_CODE_EDGE_LANE_OFF, "the edge lane is off")


def require_active(node: EdgeNode | None) -> EdgeNode:
    if node is None:
        raise _unauthorized("unknown node")
    if node.status != "active":
        raise EdgeLaneError(
            403, ERROR_CODE_EDGE_NODE_INACTIVE, f"node is {node.status}"
        )
    return node


# -- enrollment ------------------------------------------------------------


async def enroll(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    request: EnrollRequest,
    existing: EdgeNode | None,
    now: datetime,
) -> EnrollResponse:
    require_shadow(settings)
    node_id = node_id_for(request.public_key_hex)
    operator = node_id
    hotkey: str | None = None
    binding = request.hotkey_binding
    if binding is not None:
        issued = _dt(binding.issued_at)
        try:
            message = binding_message(
                node_id=node_id,
                hotkey_ss58=binding.hotkey_ss58,
                issued_at=binding.issued_at,
            )
        except SignatureError as exc:
            raise EdgeLaneError(422, ERROR_CODE_EDGE_BAD_REQUEST, str(exc)) from exc
        fresh = now - BINDING_MAX_AGE <= issued <= now + timedelta(minutes=5)
        if not fresh or not verify_signature(
            signer=binding.hotkey_ss58,
            payload=message,
            signature_hex=binding.signature_hex,
        ):
            raise EdgeLaneError(
                422, ERROR_CODE_EDGE_BAD_REQUEST, "hotkey binding does not verify"
            )
        operator = binding.hotkey_ss58
        hotkey = binding.hotkey_ss58
    quota = min(request.quota_bytes, settings.node_quota_cap_bytes)
    platform = request.platform.model_dump(mode="json")
    node = existing
    if node is None or node.status == "retired":
        # Admit one node at a time: two enrollments must not both take the last
        # slot, the same key enrolling twice must not insert twice, and a
        # retired node coming back takes a slot like a new one.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtextextended(ADMISSION_LOCK_KEY, 0))
            )
        )
        node = await session.get(EdgeNode, node_id, populate_existing=True)
        if node is None or node.status == "retired":
            enrolled = await session.scalar(
                select(func.count())
                .select_from(EdgeNode)
                .where(EdgeNode.status != "retired")
            )
            if (enrolled or 0) >= settings.max_nodes:
                raise EdgeLaneError(
                    503, ERROR_CODE_EDGE_LANE_FULL, "the edge lane is full"
                )
    if node is None:
        node = EdgeNode(
            node_id=node_id,
            public_key_hex=request.public_key_hex,
            operator=operator,
            hotkey_ss58=hotkey,
            client_name=request.client.name,
            client_version=request.client.version,
            platform=platform,
            quota_bytes=quota,
            status="active",
            enrolled_at=now,
        )
        session.add(node)
    else:
        if node.status == "suspended":
            raise EdgeLaneError(403, ERROR_CODE_EDGE_NODE_INACTIVE, "node is suspended")
        node.status = "active"
        node.quota_bytes = quota
        node.client_name = request.client.name
        node.client_version = request.client.version
        node.platform = platform
        if binding is not None:
            node.operator = operator
            node.hotkey_ss58 = hotkey
    await session.flush()
    return EnrollResponse(
        node_id=node_id,
        status="active",
        limits=settings.lane().limits(),
        server_time=int(_ts(now)),
    )


# -- the lane salt -----------------------------------------------------------


async def placement_salt(session: AsyncSession) -> bytes:
    """The secret rendezvous salt, created once on first use."""
    await session.execute(
        insert(EdgeLaneState)
        .values(id=1, placement_salt=secrets.token_bytes(32))
        .on_conflict_do_nothing()
    )
    salt = await session.scalar(
        select(EdgeLaneState.placement_salt).where(EdgeLaneState.id == 1)
    )
    if salt is None:  # pragma: no cover - the insert above guarantees the row
        raise RuntimeError("edge lane placement salt is missing")
    return bytes(salt)


# -- polling ---------------------------------------------------------------


def _window(settings: EdgeLaneSettings, now: datetime) -> tuple[int, float]:
    index = int(_ts(now) // settings.window_seconds)
    return index, float(index * settings.window_seconds)


async def _held(session: AsyncSession, node_id: str, now: datetime) -> list[Held]:
    rows = await session.execute(
        select(EdgeAssignment.digest, EdgeObject.size_bytes)
        .join(EdgeObject, EdgeObject.digest == EdgeAssignment.digest)
        .where(
            EdgeAssignment.node_id == node_id,
            EdgeAssignment.acked_at.is_not(None),
            EdgeAssignment.released_at.is_(None),
            EdgeAssignment.lease_expires_at > now,
            EdgeObject.revoked_at.is_(None),
        )
        .order_by(EdgeAssignment.digest)
    )
    return [Held(digest, size) for digest, size in rows.all()]


async def _recently_revoked(
    session: AsyncSession, node_id: str, now: datetime, lookback: int
) -> list[Held]:
    """Objects this node still held when they were revoked (fair checks only)."""
    rows = await session.execute(
        select(EdgeAssignment.digest, EdgeObject.size_bytes)
        .join(EdgeObject, EdgeObject.digest == EdgeAssignment.digest)
        .where(
            EdgeAssignment.node_id == node_id,
            EdgeAssignment.acked_at.is_not(None),
            EdgeObject.revoked_at.is_not(None),
            EdgeObject.revoked_at >= now - timedelta(seconds=lookback),
            (EdgeAssignment.released_at.is_(None))
            | (EdgeAssignment.released_at >= EdgeObject.revoked_at),
            EdgeAssignment.lease_expires_at >= EdgeObject.revoked_at,
        )
        .distinct()
        .order_by(EdgeAssignment.digest)
    )
    return [Held(digest, size) for digest, size in rows.all()]


async def _issue_due(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    node_id: str,
    now: datetime,
    rng: Random,
) -> list[Challenge]:
    index, start = _window(settings, now)
    slots = list(
        await session.scalars(
            select(EdgeChallengeSlot)
            .where(
                EdgeChallengeSlot.node_id == node_id,
                EdgeChallengeSlot.window_index == index,
            )
            .order_by(EdgeChallengeSlot.due_at)
        )
    )
    if not slots:
        for slot in schedule_window(
            window_start=start,
            window_seconds=settings.window_seconds,
            slots=settings.challenges_per_window,
            retrieval_share=settings.retrieval_share,
            revoked_share=settings.revoked_share,
            rng=rng,
        ):
            row = EdgeChallengeSlot(
                node_id=node_id,
                window_index=index,
                due_at=_dt(slot.due_at),
                purpose=slot.purpose,
                state="scheduled",
                outcome="pending",
            )
            session.add(row)
            slots.append(row)
        await session.flush()
    # A slot due late in an earlier window stays live within grace: a node
    # polling just past a boundary must get it, or an online node would miss
    # late slots at every window edge. Grace can span several windows.
    lane = settings.lane()
    late = list(
        await session.scalars(
            select(EdgeChallengeSlot)
            .where(
                EdgeChallengeSlot.node_id == node_id,
                EdgeChallengeSlot.window_index >= index - lane.late_slot_windows,
                EdgeChallengeSlot.window_index < index,
                EdgeChallengeSlot.state == "scheduled",
                EdgeChallengeSlot.due_at <= now,
            )
            .order_by(EdgeChallengeSlot.due_at)
        )
    )
    grace = timedelta(seconds=lane.grace_seconds)
    # Challenges issued earlier and still undecided go out again until their
    # deadline, so a lost poll response or a failed answer costs a node
    # nothing. Answering twice changes nothing: the first answer decides. A
    # live one was issued within grace of its slot and a deadline ago at most.
    # Due slots come first, so a redelivery never pushes one past grace.
    reach = -(
        -(lane.grace_seconds + settings.challenge_deadline_seconds)
        // settings.window_seconds
    )
    earlier = await session.scalars(
        select(EdgeChallengeSlot)
        .where(
            EdgeChallengeSlot.node_id == node_id,
            EdgeChallengeSlot.window_index >= index - reach,
            EdgeChallengeSlot.state == "issued",
            EdgeChallengeSlot.outcome == "pending",
            EdgeChallengeSlot.deadline_at > now,
        )
        .order_by(EdgeChallengeSlot.issued_at, EdgeChallengeSlot.id)
        .limit(MAX_CHALLENGES_PER_POLL)
    )
    redeliver = [_wire(slot) for slot in earlier]
    issued: list[Challenge] = []
    held: list[Held] | None = None
    revoked: list[Held] | None = None
    for slot in [*late, *slots]:
        if slot.state != "scheduled" or slot.due_at > now:
            continue
        if now - slot.due_at > grace:
            slot.state = "missed"
            slot.outcome = "missed"
            continue
        if len(issued) >= MAX_CHALLENGES_PER_POLL:
            break
        if held is None:
            held = await _held(session, node_id, now)
            revoked = await _recently_revoked(
                session, node_id, now, settings.revoked_lookback_seconds
            )
        drawn = draw_challenge(
            purpose=slot.purpose,  # type: ignore[arg-type]
            held=held,
            revoked=revoked or [],
            rng=rng,
            nonce=secrets.token_bytes,
        )
        if drawn is None:
            slot.state = "void"
            slot.outcome = "void"
            continue
        challenge_id = secrets.token_hex(16)
        deadline = now + timedelta(seconds=settings.challenge_deadline_seconds)
        # Record what was actually asked: a revoked slot with nothing revoked
        # to ask about became a possession challenge.
        slot.purpose = drawn.purpose
        slot.state = "issued"
        slot.challenge_id = challenge_id
        slot.kind = drawn.kind
        slot.digest = drawn.digest
        slot.range_offset = drawn.offset
        slot.range_length = drawn.length
        slot.nonce_hex = drawn.nonce_hex
        slot.issued_at = now
        slot.deadline_at = deadline
        issued.append(_wire(slot))
    return issued + redeliver[: MAX_CHALLENGES_PER_POLL - len(issued)]


def _wire(slot: EdgeChallengeSlot) -> Challenge:
    """The challenge an issued slot carries, as the node receives it."""
    if (
        slot.challenge_id is None
        or slot.kind is None
        or slot.digest is None
        or slot.range_offset is None
        or slot.range_length is None
        or slot.deadline_at is None
    ):  # pragma: no cover - an issued slot always carries its challenge
        raise RuntimeError(f"issued edge challenge slot {slot.id} is incomplete")
    return Challenge(
        challenge_id=slot.challenge_id,
        kind=slot.kind,  # type: ignore[arg-type]
        digest=slot.digest,
        nonce_hex=slot.nonce_hex,
        offset=slot.range_offset,
        length=slot.range_length,
        deadline_at=int(_ts(slot.deadline_at)),
    )


async def _place(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    node: EdgeNode,
    request: PollRequest,
    now: datetime,
    salt: bytes,
) -> set[str]:
    """Open this node's rendezvous share; return the digests to list.

    Objects the node no longer ranks for are not listed: their leases lapse
    and the node deletes them. A span whose lease lapsed (the node stopped
    polling) closes at the lapse, so the gap is never credited. If the object
    is still the node's, a new span opens and the node acknowledges it again.
    """
    lease = timedelta(seconds=settings.lease_seconds)
    live_rows = await session.execute(
        select(EdgeNode.node_id, EdgeNode.operator).where(
            EdgeNode.status == "active",
            (EdgeNode.node_id == node.node_id) | (EdgeNode.last_poll_at >= now - lease),
        )
    )
    live = [PlacementNode(node_id, operator) for node_id, operator in live_rows.all()]
    object_rows = await session.execute(
        select(EdgeObject.digest, EdgeObject.size_bytes, EdgeObject.created_at).where(
            EdgeObject.revoked_at.is_(None)
        )
    )
    objects = [
        PlacementObject(digest, size, _ts(created))
        for digest, size, created in object_rows.all()
    ]
    sizes = {obj.digest: obj.size_bytes for obj in objects}
    # Spans of revoked objects stay open until the node confirms the deletion.
    # Any other span whose lease lapsed closes at the lapse. The node has not
    # polled since, so it has not deleted those bytes: reopening one carries
    # its fill history and needs no download, and acknowledged ones still
    # count toward the node's quota.
    open_spans: dict[str, EdgeAssignment] = {}
    lapsed: dict[str, EdgeAssignment] = {}
    for span in await session.scalars(
        select(EdgeAssignment).where(
            EdgeAssignment.node_id == node.node_id,
            EdgeAssignment.released_at.is_(None),
        )
    ):
        if span.digest in sizes and span.lease_expires_at <= now:
            span.released_at = span.lease_expires_at
            lapsed[span.digest] = span
        else:
            open_spans[span.digest] = span
    # Close before reopening: one open span per node and object.
    await session.flush()
    held_bytes = sum(
        sizes.get(digest, 0)
        for digest, span in [*open_spans.items(), *lapsed.items()]
        if span.acked_at is not None
    )
    quota = min(node.quota_bytes, held_bytes + request.free_bytes)
    desired = desired_objects(
        node_id=node.node_id,
        objects=objects,
        nodes=live,
        replicas=settings.replicas,
        salt=salt,
        quota_bytes=quota,
        max_object_bytes=settings.max_object_bytes,
    )
    listed: set[str] = set()
    to_open: list[str] = []
    created = 0
    for digest in desired:
        if len(listed) >= MAX_ASSIGNMENTS_PER_POLL:
            break
        if digest in open_spans:
            listed.add(digest)
            continue
        # Only a span that needs a download counts against the cap.
        reopened = digest in lapsed and lapsed[digest].fill_count > 0
        if not reopened and created >= settings.max_new_assignments_per_poll:
            continue
        to_open.append(digest)
        listed.add(digest)
        if not reopened:
            created += 1
    # This node may have downloaded these bytes before: a new span carries the
    # fill history of its latest span, so bytes it kept can be acknowledged
    # without a second download.
    fills = await _latest_fills(
        session, node.node_id, [digest for digest in to_open if digest not in lapsed]
    )
    for digest in to_open:
        previous = lapsed.get(digest)
        fill_count, last_filled_at = (
            (previous.fill_count, previous.last_filled_at)
            if previous is not None
            else fills.get(digest, (0, None))
        )
        span = EdgeAssignment(
            node_id=node.node_id,
            digest=digest,
            assigned_at=now,
            lease_expires_at=now + lease,
            fill_count=fill_count,
            last_filled_at=last_filled_at,
        )
        session.add(span)
        open_spans[digest] = span
    await session.flush()
    return listed


async def _latest_fills(
    session: AsyncSession, node_id: str, digests: list[str]
) -> dict[str, tuple[int, datetime | None]]:
    """Per object, the fill history of this node's latest span, if it lapsed.

    A span closed at its lapse has ``released_at`` equal to its lease expiry;
    only that path writes it. A span the node released starts fresh, because
    the node deleted the bytes.
    """
    if not digests:
        return {}
    rows = await session.execute(
        select(
            EdgeAssignment.digest,
            EdgeAssignment.fill_count,
            EdgeAssignment.last_filled_at,
            EdgeAssignment.released_at == EdgeAssignment.lease_expires_at,
        )
        .where(
            EdgeAssignment.node_id == node_id,
            EdgeAssignment.digest.in_(digests),
            EdgeAssignment.released_at.is_not(None),
        )
        .distinct(EdgeAssignment.digest)
        .order_by(EdgeAssignment.digest, EdgeAssignment.id.desc())
    )
    return {
        digest: (fill_count, last_filled_at)
        for digest, fill_count, last_filled_at, lapsed in rows.tuples().all()
        if lapsed
    }


async def poll(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    settings_revision: int,
    node_id: str,
    request: PollRequest,
    now: datetime,
    rng: Random | None = None,
) -> PollResponse:
    require_shadow(settings)
    node = await session.get(EdgeNode, node_id, with_for_update=True)
    node = require_active(node)
    node.last_poll_at = now
    await close_windows(
        session, settings=settings, settings_revision=settings_revision, now=now
    )
    challenges = await _issue_due(
        session,
        settings=settings,
        node_id=node.node_id,
        now=now,
        rng=rng if rng is not None else SystemRandom(),
    )
    listed = await _place(
        session,
        settings=settings,
        node=node,
        request=request,
        now=now,
        salt=await placement_salt(session),
    )
    lease = now + timedelta(seconds=settings.lease_seconds)
    rows = await session.execute(
        select(EdgeAssignment, EdgeObject)
        .join(EdgeObject, EdgeObject.digest == EdgeAssignment.digest)
        .where(
            EdgeAssignment.node_id == node.node_id,
            EdgeAssignment.released_at.is_(None),
        )
        .order_by(EdgeAssignment.assigned_at, EdgeAssignment.digest)
    )
    assignments: list[Assignment] = []
    revocations: list[str] = []
    for span, obj in rows.all():
        if obj.revoked_at is not None:
            revocations.append(span.digest)
            continue
        if span.digest not in listed:
            continue
        span.lease_expires_at = lease
        assignments.append(
            Assignment(
                digest=span.digest,
                size_bytes=obj.size_bytes,
                media_type=obj.media_type,
                lease_expires_at=int(_ts(lease)),
            )
        )
    index, _ = _window(settings, now)
    return PollResponse(
        server_time=int(_ts(now)),
        window_index=index,
        status="active",
        limits=settings.lane().limits(),
        assignments=assignments[:MAX_ASSIGNMENTS_PER_POLL],
        revocations=revocations[:MAX_ASSIGNMENTS_PER_POLL],
        challenges=challenges,
        next_poll_seconds=settings.poll_interval_seconds,
    )


# -- fill, acknowledge, answer, status, leave ---------------------------------


async def fill_target(
    session: AsyncSession, *, node: EdgeNode | None, digest: str
) -> EdgeObject:
    """The object this node may download: assigned to it and not revoked."""
    node = require_active(node)
    span = await session.scalar(
        select(EdgeAssignment.id).where(
            EdgeAssignment.node_id == node.node_id,
            EdgeAssignment.digest == digest,
            EdgeAssignment.released_at.is_(None),
        )
    )
    if span is None:
        raise EdgeLaneError(
            404, ERROR_CODE_EDGE_NOT_ASSIGNED, "not assigned to this node"
        )
    obj = await session.get(EdgeObject, digest)
    if obj is None or obj.revoked_at is not None:
        raise EdgeLaneError(410, ERROR_CODE_EDGE_REVOKED, "object revoked")
    return obj


async def record_fill(
    session: AsyncSession, *, node_id: str, digest: str, now: datetime
) -> None:
    """Log one completed download on the node's open span for the object."""
    await session.execute(
        update(EdgeAssignment)
        .where(
            EdgeAssignment.node_id == node_id,
            EdgeAssignment.digest == digest,
            EdgeAssignment.released_at.is_(None),
        )
        .values(fill_count=EdgeAssignment.fill_count + 1, last_filled_at=now)
    )


async def acknowledge(
    session: AsyncSession, *, node: EdgeNode | None, request: AckRequest, now: datetime
) -> AckResponse:
    node = require_active(node)
    digests = sorted(set(request.held) | set(request.released))
    spans = {
        span.digest: span
        for span in await session.scalars(
            select(EdgeAssignment)
            .where(
                EdgeAssignment.node_id == node.node_id,
                EdgeAssignment.digest.in_(digests),
                EdgeAssignment.released_at.is_(None),
            )
            .with_for_update()
        )
    }
    accepted = ignored = 0
    for digest in request.held:
        span = spans.get(digest)
        if span is None or span.fill_count == 0 or span.released_at is not None:
            ignored += 1
            continue
        if span.acked_at is None:
            span.acked_at = now
        accepted += 1
    for digest in request.released:
        span = spans.get(digest)
        if span is None or span.released_at is not None:
            ignored += 1
            continue
        span.released_at = now
        accepted += 1
    return AckResponse(accepted=accepted, ignored=ignored)


@dataclass(frozen=True)
class PendingAnswer:
    """Everything needed to judge an answer once the object bytes are loaded."""

    slot_id: int
    challenge: DrawnChallenge
    issued_at: float
    deadline_at: float
    last_filled_at: float | None


async def pending_answer(
    session: AsyncSession, *, node: EdgeNode | None, challenge_id: str
) -> PendingAnswer | None:
    """The challenge to judge, or ``None`` when it was already answered."""
    node = require_active(node)
    slot = await session.scalar(
        select(EdgeChallengeSlot)
        .where(
            EdgeChallengeSlot.challenge_id == challenge_id,
            EdgeChallengeSlot.node_id == node.node_id,
        )
        .with_for_update()
    )
    if slot is None:
        raise EdgeLaneError(404, ERROR_CODE_EDGE_UNKNOWN_CHALLENGE, "unknown challenge")
    if slot.outcome != "pending":
        return None
    if (
        slot.digest is None
        or slot.kind is None
        or slot.issued_at is None
        or slot.deadline_at is None
        or slot.range_offset is None
        or slot.range_length is None
    ):  # pragma: no cover - an issued slot always carries its challenge
        raise RuntimeError(f"issued edge challenge {challenge_id} is incomplete")
    last_filled = await session.scalar(
        select(func.max(EdgeAssignment.last_filled_at)).where(
            EdgeAssignment.node_id == node.node_id,
            EdgeAssignment.digest == slot.digest,
        )
    )
    return PendingAnswer(
        slot_id=slot.id,
        challenge=DrawnChallenge(
            purpose=slot.purpose,  # type: ignore[arg-type]
            kind=slot.kind,  # type: ignore[arg-type]
            digest=slot.digest,
            offset=slot.range_offset,
            length=slot.range_length,
            nonce_hex=slot.nonce_hex,
        ),
        issued_at=_ts(slot.issued_at),
        deadline_at=_ts(slot.deadline_at),
        last_filled_at=_ts(last_filled) if last_filled is not None else None,
    )


async def record_answer(
    session: AsyncSession,
    *,
    pending: PendingAnswer,
    data: bytes,
    answer: ChallengeAnswer,
    now: datetime,
) -> Outcome:
    outcome = judge_answer(
        challenge=pending.challenge,
        data=data,
        answer=answer,
        issued_at=pending.issued_at,
        answered_at=_ts(now),
        deadline_at=pending.deadline_at,
        last_filled_at=pending.last_filled_at,
    )
    await session.execute(
        update(EdgeChallengeSlot)
        .where(
            EdgeChallengeSlot.id == pending.slot_id,
            EdgeChallengeSlot.outcome == "pending",
        )
        .values(outcome=outcome, answered_at=now)
    )
    return outcome


async def status(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    node: EdgeNode | None,
    now: datetime,
) -> NodeStatusResponse:
    if node is None:
        raise _unauthorized("unknown node")
    held = await _held(session, node.node_id, now)
    index, _ = _window(settings, now)
    slots = list(
        await session.scalars(
            select(EdgeChallengeSlot).where(
                EdgeChallengeSlot.node_id == node.node_id,
                EdgeChallengeSlot.window_index == index,
            )
        )
    )
    summary = None
    if slots:  # the window is scheduled, even if nothing is due yet
        outcomes: list[str] = []
        for slot in slots:
            if slot.state == "scheduled":
                continue
            outcome = slot.outcome
            if (
                outcome == "pending"
                and slot.deadline_at is not None
                and now > slot.deadline_at
            ):
                outcome = "missed"
            outcomes.append(outcome)
        counted = [o for o in outcomes if o != "void"]
        summary = WindowSummary(
            window_index=index,
            scheduled=len(counted),
            passed=sum(o in ("passed", "refused_ok") for o in counted),
            missed=sum(o == "missed" for o in counted),
            failed=sum(o in ("failed", "violation", "refilled") for o in counted),
        )
    return NodeStatusResponse(
        node_id=node.node_id,
        status=node.status,  # type: ignore[arg-type]
        mode=settings.mode,
        held_objects=len(held),
        held_bytes=sum(h.size_bytes for h in held),
        current_window=summary,
    )


async def leave(session: AsyncSession, *, node: EdgeNode | None, now: datetime) -> None:
    if node is None:
        raise _unauthorized("unknown node")
    node.status = "retired"
    await session.execute(
        update(EdgeAssignment)
        .where(
            EdgeAssignment.node_id == node.node_id,
            EdgeAssignment.released_at.is_(None),
        )
        .values(released_at=now)
    )


# -- the ledger --------------------------------------------------------------


def _earliest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


async def close_windows(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    settings_revision: int,
    now: datetime,
) -> list[int]:
    """Score every closed window that has no ledger yet. Insert-once.

    A window is closed once it has ended and every deadline in it has passed.
    Whichever caller first inserts the closure row writes the ledger rows, in
    the same transaction, so concurrent callers never score a window twice.
    """
    lane = settings.lane()
    horizon = _ts(now) - settings.challenge_deadline_seconds - lane.grace_seconds
    newest_closed = int(horizon // settings.window_seconds) - 1
    candidates = list(
        await session.scalars(
            select(EdgeChallengeSlot.window_index)
            .where(
                EdgeChallengeSlot.window_index <= newest_closed,
                ~select(EdgeWindowClosure.window_index)
                .where(EdgeWindowClosure.window_index == EdgeChallengeSlot.window_index)
                .exists(),
            )
            .group_by(EdgeChallengeSlot.window_index)
            .order_by(EdgeChallengeSlot.window_index)
            .limit(MAX_WINDOWS_PER_CLOSE)
        )
    )
    closed: list[int] = []
    for index in candidates:
        claimed = await session.scalar(
            insert(EdgeWindowClosure)
            .values(
                window_index=index,
                settings_revision=settings_revision,
                node_count=0,
                closed_at=now,
            )
            .on_conflict_do_nothing()
            .returning(EdgeWindowClosure.window_index)
        )
        if claimed is None:
            continue
        count = await _score_window(
            session,
            settings=settings,
            settings_revision=settings_revision,
            index=index,
            now=now,
        )
        await session.execute(
            update(EdgeWindowClosure)
            .where(EdgeWindowClosure.window_index == index)
            .values(node_count=count)
        )
        closed.append(index)
    return closed


async def _score_window(
    session: AsyncSession,
    *,
    settings: EdgeLaneSettings,
    settings_revision: int,
    index: int,
    now: datetime,
) -> int:
    start = _dt(float(index * settings.window_seconds))
    end = start + timedelta(seconds=settings.window_seconds)
    slots = list(
        await session.scalars(
            select(EdgeChallengeSlot)
            .where(EdgeChallengeSlot.window_index == index)
            .order_by(EdgeChallengeSlot.node_id, EdgeChallengeSlot.id)
        )
    )
    by_node: dict[str, list[EdgeChallengeSlot]] = {}
    for slot in slots:
        by_node.setdefault(slot.node_id, []).append(slot)
    if not by_node:
        return 0
    span_rows = await session.execute(
        select(EdgeAssignment, EdgeObject.size_bytes, EdgeObject.revoked_at)
        .join(EdgeObject, EdgeObject.digest == EdgeAssignment.digest)
        .where(
            EdgeAssignment.node_id.in_(list(by_node)),
            EdgeAssignment.acked_at.is_not(None),
            EdgeAssignment.acked_at < end,
        )
    )
    holds: dict[str, list[HoldInterval]] = {}
    for span, size, revoked_at in span_rows.all():
        if span.acked_at is None:
            continue
        lapse = span.lease_expires_at if span.lease_expires_at <= now else None
        ended = _earliest(span.released_at, revoked_at, lapse)
        holds.setdefault(span.node_id, []).append(
            HoldInterval(
                size_bytes=size,
                start=_ts(span.acked_at),
                end=_ts(ended) if ended is not None else None,
            )
        )
    operator_rows = await session.execute(
        select(EdgeNode.node_id, EdgeNode.operator).where(
            EdgeNode.node_id.in_(list(by_node))
        )
    )
    operators: dict[str, str] = dict(operator_rows.tuples().all())
    scores: dict[str, WindowScore] = {}
    for node_id, node_slots in by_node.items():
        observations = [
            Observation(
                slot.purpose,  # type: ignore[arg-type]
                _closed_outcome(slot),
            )
            for slot in node_slots
        ]
        scores[node_id] = score_window(
            observations=observations,
            holds=holds.get(node_id, []),
            window_start=_ts(start),
            window_end=_ts(end),
            params=settings.lane().ledger_params(),
        )
    shares = hypothetical_shares(
        {node_id: score.verified_units for node_id, score in scores.items()}
    )
    for node_id, score in scores.items():
        session.add(
            EdgeLedgerWindow(
                window_index=index,
                node_id=node_id,
                operator=operators.get(node_id, node_id),
                scheduled=score.scheduled,
                passed=score.passed,
                failed=score.failed,
                missed=score.missed,
                refilled=score.refilled,
                refused=score.refused,
                revocation_checks=score.revocation_checks,
                revocation_violations=score.revocation_violations,
                held_gib_hours=score.held_gib_hours,
                availability=score.availability,
                eligible=score.eligible,
                verified_units=score.verified_units,
                hypothetical_share=shares[node_id],
                reasons=list(score.reasons),
                settings_revision=settings_revision,
                weight_eligible=False,
                computed_at=now,
            )
        )
    await session.flush()
    return len(scores)


def _closed_outcome(slot: EdgeChallengeSlot) -> Outcome:
    if slot.state == "issued":
        return slot.outcome  # type: ignore[return-value]
    if slot.state == "void":
        return "void"
    return "missed"
