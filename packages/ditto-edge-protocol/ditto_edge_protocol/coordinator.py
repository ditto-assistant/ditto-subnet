"""An in-memory edge/v1 coordinator: Platform's lane semantics without a DB.

The dry run and the tests drive real nodes against it. Platform implements
the same rules with Postgres; both call the same pure functions for
placement, challenge scheduling and judging, and the ledger, so they cannot
disagree about what a node earned.

Server-side rules, per poll:

1. **Schedule.** The node's first poll of a window draws that window's
   challenge slots (:func:`~ditto_edge_protocol.challenges.schedule_window`).
   Slots already more than ``grace`` in the past are missed.
2. **Due slots.** A due slot within ``grace`` is issued. The challenge targets
   an acknowledged object, or a recently revoked one for a ``revoked`` slot.
   With nothing to challenge, the slot is void. A slot older than ``grace`` is
   missed.
3. **Placement.** The node gets new objects up to its quota, least-replicated
   first; replica counts include live leases only.
4. **Leases and revocations.** Every unrevoked assignment gets a fresh lease,
   and every revoked one the node has not released yet is listed for
   deletion.

A window is scored once it is closed and every deadline in it has passed. Rows
are insert-once.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from random import Random, SystemRandom

from ditto_edge_protocol import paths
from ditto_edge_protocol.challenges import (
    DrawnChallenge,
    Held,
    Outcome,
    Purpose,
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
from ditto_edge_protocol.limits import MAX_ASSIGNMENTS_PER_POLL, MEDIA_TYPES
from ditto_edge_protocol.placement import (
    PlacementNode,
    PlacementObject,
    desired_objects,
)
from ditto_edge_protocol.proofs import digest_of
from ditto_edge_protocol.settings import LaneSettings
from ditto_edge_protocol.signing import (
    HEADER_NODE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SignatureError,
    node_id_for,
    verify_request,
)
from ditto_edge_protocol.wire import (
    AckRequest,
    AckResponse,
    AnswerResponse,
    Assignment,
    Challenge,
    ChallengeAnswer,
    EnrollRequest,
    EnrollResponse,
    HotkeyBinding,
    LeaveRequest,
    NodeStatusResponse,
    PollRequest,
    PollResponse,
    WindowSummary,
)

BindingVerifier = Callable[[HotkeyBinding, str], bool]
"""Checks an sr25519 hotkey binding for a node id. Platform supplies one."""


class CoordinatorError(Exception):
    """A request the lane refuses. ``code`` maps to an HTTP status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Node:
    node_id: str
    public_key_hex: str
    quota_bytes: int
    operator: str
    status: str
    enrolled_at: float
    last_poll_at: float | None = None


@dataclass
class _Object:
    digest: str
    data: bytes
    media_type: str
    source: str
    created_at: float
    revoked_at: float | None = None
    revoke_reason: str = ""


@dataclass
class _Assignment:
    node_id: str
    digest: str
    assigned_at: float
    lease_expires_at: float
    fill_count: int = 0
    last_filled_at: float | None = None
    acked_at: float | None = None
    released_at: float | None = None


@dataclass
class _Slot:
    due_at: float
    purpose: Purpose
    state: str = "scheduled"
    challenge_id: str | None = None


@dataclass
class _Challenge:
    challenge_id: str
    node_id: str
    window_index: int
    drawn: DrawnChallenge
    issued_at: float
    deadline_at: float
    outcome: Outcome = "pending"
    answered_at: float | None = None


@dataclass(frozen=True)
class LedgerRow:
    window_index: int
    node_id: str
    operator: str
    score: WindowScore
    hypothetical_share: float
    weight_eligible: bool = False


@dataclass
class _NonceLog:
    seen: dict[tuple[str, str], float] = field(default_factory=dict)

    def consume(self, node_id: str, nonce: str, now: float, horizon: float) -> bool:
        if len(self.seen) > 50_000:
            self.seen = {k: t for k, t in self.seen.items() if now - t < horizon}
        key = (node_id, nonce)
        if key in self.seen:
            return False
        self.seen[key] = now
        return True


class Coordinator:
    def __init__(
        self,
        settings: LaneSettings,
        *,
        clock: Callable[[], float],
        rng: Random | None = None,
        nonce: Callable[[int], bytes] = secrets.token_bytes,
        binding_verifier: BindingVerifier | None = None,
        placement_salt: bytes | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.rng = rng if rng is not None else SystemRandom()
        self.nonce = nonce
        self.binding_verifier = binding_verifier
        self.placement_salt = (
            placement_salt if placement_salt is not None else nonce(32)
        )
        self.nodes: dict[str, _Node] = {}
        self.objects: dict[str, _Object] = {}
        self.assignments: dict[tuple[str, str], _Assignment] = {}
        self.slots: dict[tuple[str, int], list[_Slot]] = {}
        self.challenges: dict[str, _Challenge] = {}
        self.ledger: list[LedgerRow] = []
        self._scored_windows: set[int] = set()
        self._nonces = _NonceLog()

    # -- authentication ---------------------------------------------------

    def authenticate(
        self, *, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> str:
        """Verify a signed request and return its node id.

        Enrollment is self-certifying: the key travels in the body and must
        hash to the claimed node id. Every other path needs an enrolled node.
        """
        node_id = headers.get(HEADER_NODE, "")
        try:
            timestamp = int(headers.get(HEADER_TIMESTAMP, ""))
        except ValueError as exc:
            raise CoordinatorError("unauthorized", "missing timestamp") from exc
        nonce = headers.get(HEADER_NONCE, "")
        signature = headers.get(HEADER_SIGNATURE, "")
        if path == paths.ENROLL:
            try:
                public_key = json.loads(body)["public_key_hex"]
            except (ValueError, KeyError, TypeError) as exc:
                raise CoordinatorError("bad_request", "enrollment needs a key") from exc
            if not isinstance(public_key, str) or node_id_for(public_key) != node_id:
                raise CoordinatorError("unauthorized", "node id does not match key")
        else:
            node = self.nodes.get(node_id)
            if node is None:
                raise CoordinatorError("unauthorized", "unknown node")
            public_key = node.public_key_hex
        now = self.clock()
        try:
            verify_request(
                public_key_hex=public_key,
                signature_hex=signature,
                method=method,
                path=path,
                timestamp=timestamp,
                nonce_hex=nonce,
                body=body,
                now=int(now),
            )
        except SignatureError as exc:
            raise CoordinatorError("unauthorized", str(exc)) from exc
        if not self._nonces.consume(node_id, nonce, now, horizon=600.0):
            raise CoordinatorError("unauthorized", "nonce replayed")
        return node_id

    # -- operator actions ---------------------------------------------------

    def ingest(self, data: bytes, media_type: str, source: str = "synthetic") -> str:
        if media_type not in MEDIA_TYPES:
            media_type = "application/octet-stream"
        if not 0 < len(data) <= self.settings.max_object_bytes:
            raise CoordinatorError("bad_request", "object size outside the lane limit")
        digest = digest_of(data)
        if digest not in self.objects:
            self.objects[digest] = _Object(
                digest=digest,
                data=data,
                media_type=media_type,
                source=source,
                created_at=self.clock(),
            )
        return digest

    def revoke(self, digest: str, reason: str) -> bool:
        obj = self.objects.get(digest)
        if obj is None or obj.revoked_at is not None:
            return False
        obj.revoked_at = self.clock()
        obj.revoke_reason = reason
        return True

    # -- node API -------------------------------------------------------------

    def _require_shadow(self) -> None:
        if self.settings.mode != "shadow":
            raise CoordinatorError("lane_off", "the edge lane is off")

    def _require_active(self, node_id: str) -> _Node:
        node = self.nodes.get(node_id)
        if node is None:
            raise CoordinatorError("unauthorized", "unknown node")
        if node.status != "active":
            raise CoordinatorError("forbidden", f"node is {node.status}")
        return node

    def enroll(self, request: EnrollRequest) -> EnrollResponse:
        self._require_shadow()
        node_id = node_id_for(request.public_key_hex)
        now = self.clock()
        node = self.nodes.get(node_id)
        if node is None:
            if len(self.nodes) >= self.settings.max_nodes:
                raise CoordinatorError("capacity", "the shadow lane is full")
            operator = node_id
            binding = request.hotkey_binding
            if (
                binding is not None
                and self.binding_verifier is not None
                and self.binding_verifier(binding, node_id)
            ):
                operator = binding.hotkey_ss58
            node = _Node(
                node_id=node_id,
                public_key_hex=request.public_key_hex,
                quota_bytes=min(
                    request.quota_bytes, self.settings.node_quota_cap_bytes
                ),
                operator=operator,
                status="active",
                enrolled_at=now,
            )
            self.nodes[node_id] = node
        elif node.status == "retired":
            node.status = "active"
        node.quota_bytes = min(request.quota_bytes, self.settings.node_quota_cap_bytes)
        return EnrollResponse(
            node_id=node_id,
            status=node.status,  # type: ignore[arg-type]
            limits=self.settings.limits(),
            server_time=int(now),
        )

    def _window(self, now: float) -> tuple[int, float]:
        index = int(now // self.settings.window_seconds)
        return index, float(index * self.settings.window_seconds)

    def _held(self, node_id: str, now: float) -> list[Held]:
        """Acknowledged objects under a live lease: what a node can be asked about."""
        return sorted(
            (
                Held(a.digest, len(self.objects[a.digest].data))
                for a in self.assignments.values()
                if a.node_id == node_id
                and a.acked_at is not None
                and a.released_at is None
                and a.lease_expires_at > now
                and self.objects[a.digest].revoked_at is None
            ),
            key=lambda held: held.digest,
        )

    def _recently_revoked(self, node_id: str, now: float) -> list[Held]:
        """Objects this node still held when they were revoked.

        An object the node had already let lapse before the revocation is not
        a fair check: ``not_held`` is the honest answer for it either way.
        """
        horizon = now - self.settings.revoked_lookback_seconds
        return sorted(
            (
                Held(a.digest, len(self.objects[a.digest].data))
                for a in self.assignments.values()
                if a.node_id == node_id
                and a.acked_at is not None
                and (revoked_at := self.objects[a.digest].revoked_at) is not None
                and revoked_at >= horizon
                and (a.released_at is None or a.released_at >= revoked_at)
                and a.lease_expires_at >= revoked_at
            ),
            key=lambda held: held.digest,
        )

    def _issue_due(self, node_id: str, now: float) -> list[Challenge]:
        settings = self.settings
        index, start = self._window(now)
        schedule = self.slots.get((node_id, index))
        if schedule is None:
            schedule = [
                _Slot(due_at=slot.due_at, purpose=slot.purpose)
                for slot in schedule_window(
                    window_start=start,
                    window_seconds=settings.window_seconds,
                    slots=settings.challenges_per_window,
                    retrieval_share=settings.retrieval_share,
                    revoked_share=settings.revoked_share,
                    rng=self.rng,
                )
            ]
            self.slots[(node_id, index)] = schedule
        issued: list[Challenge] = []
        for slot in schedule:
            if slot.state != "scheduled" or slot.due_at > now:
                continue
            if now - slot.due_at > settings.grace_seconds:
                slot.state = "missed"
                continue
            drawn = draw_challenge(
                purpose=slot.purpose,
                held=self._held(node_id, now),
                revoked=self._recently_revoked(node_id, now),
                rng=self.rng,
                nonce=self.nonce,
            )
            if drawn is None:
                slot.state = "void"
                continue
            # A revoked slot with nothing revoked to ask about becomes a possession
            # challenge; the ledger must count it as what was actually asked.
            slot.purpose = drawn.purpose
            challenge_id = self.nonce(16).hex()
            deadline = now + settings.challenge_deadline_seconds
            self.challenges[challenge_id] = _Challenge(
                challenge_id=challenge_id,
                node_id=node_id,
                window_index=index,
                drawn=drawn,
                issued_at=now,
                deadline_at=deadline,
            )
            slot.state = "issued"
            slot.challenge_id = challenge_id
            issued.append(
                Challenge(
                    challenge_id=challenge_id,
                    kind=drawn.kind,
                    digest=drawn.digest,
                    nonce_hex=drawn.nonce_hex,
                    offset=drawn.offset,
                    length=drawn.length,
                    deadline_at=int(deadline),
                )
            )
        return issued

    def _live_nodes(self, node: _Node, now: float) -> list[PlacementNode]:
        """Active nodes that polled within a lease, always including ``node``."""
        lease = self.settings.lease_seconds
        return [
            PlacementNode(other.node_id, other.operator)
            for other in self.nodes.values()
            if other.status == "active"
            and (
                other.node_id == node.node_id
                or (
                    other.last_poll_at is not None and now - other.last_poll_at <= lease
                )
            )
        ]

    def _place(self, node: _Node, request: PollRequest, now: float) -> set[str]:
        """Assign this node its rendezvous share and return the digests to list.

        Objects the node no longer holds a top rank for are simply not listed:
        their leases lapse, the node deletes them, and they are marked
        released here once the lapse has passed.
        """
        settings = self.settings
        held_bytes = sum(
            len(self.objects[a.digest].data)
            for a in self.assignments.values()
            if a.node_id == node.node_id
            and a.released_at is None
            and a.acked_at is not None
            and self.objects[a.digest].revoked_at is None
        )
        quota = min(node.quota_bytes, held_bytes + request.free_bytes)
        desired = desired_objects(
            node_id=node.node_id,
            objects=[
                PlacementObject(obj.digest, len(obj.data), obj.created_at)
                for obj in self.objects.values()
                if obj.revoked_at is None
            ],
            nodes=self._live_nodes(node, now),
            replicas=settings.replicas,
            salt=self.placement_salt,
            quota_bytes=quota,
            max_object_bytes=settings.max_object_bytes,
        )
        listed: set[str] = set()
        created = 0
        for digest in desired:
            # A poll lists at most MAX_ASSIGNMENTS_PER_POLL objects. Anything
            # beyond it is neither listed nor renewed, so it lapses instead of
            # holding a lease the node never hears about.
            if len(listed) >= MAX_ASSIGNMENTS_PER_POLL:
                break
            current = self.assignments.get((node.node_id, digest))
            if current is not None and current.released_at is None:
                listed.add(digest)
                continue
            if created >= settings.max_new_assignments_per_poll:
                continue
            self.assignments[(node.node_id, digest)] = _Assignment(
                node_id=node.node_id,
                digest=digest,
                assigned_at=now,
                lease_expires_at=now + settings.lease_seconds,
            )
            listed.add(digest)
            created += 1
        for a in self.assignments.values():
            if (
                a.node_id == node.node_id
                and a.released_at is None
                and a.digest not in listed
                and self.objects[a.digest].revoked_at is None
                and a.lease_expires_at <= now
            ):
                a.released_at = a.lease_expires_at
        return listed

    def poll(self, node_id: str, request: PollRequest) -> PollResponse:
        self._require_shadow()
        node = self._require_active(node_id)
        now = self.clock()
        node.last_poll_at = now
        challenges = self._issue_due(node_id, now)
        listed = self._place(node, request, now)
        assignments: list[Assignment] = []
        revocations: list[str] = []
        lease = now + self.settings.lease_seconds
        for a in sorted(self.assignments.values(), key=lambda x: x.assigned_at):
            if a.node_id != node_id or a.released_at is not None:
                continue
            obj = self.objects[a.digest]
            if obj.revoked_at is not None:
                revocations.append(a.digest)
                continue
            if a.digest not in listed:
                continue
            a.lease_expires_at = lease
            assignments.append(
                Assignment(
                    digest=a.digest,
                    size_bytes=len(obj.data),
                    media_type=obj.media_type,
                    lease_expires_at=int(lease),
                )
            )
        index, _ = self._window(now)
        return PollResponse(
            server_time=int(now),
            window_index=index,
            status="active",
            limits=self.settings.limits(),
            assignments=assignments[:MAX_ASSIGNMENTS_PER_POLL],
            revocations=revocations[:MAX_ASSIGNMENTS_PER_POLL],
            challenges=challenges,
            next_poll_seconds=self.settings.poll_interval_seconds,
        )

    def fill(self, node_id: str, digest: str) -> bytes:
        self._require_shadow()
        self._require_active(node_id)
        assignment = self.assignments.get((node_id, digest))
        if assignment is None or assignment.released_at is not None:
            raise CoordinatorError("not_found", "not assigned to this node")
        obj = self.objects[digest]
        if obj.revoked_at is not None:
            raise CoordinatorError("gone", "object revoked")
        assignment.fill_count += 1
        assignment.last_filled_at = self.clock()
        return obj.data

    def ack(self, node_id: str, request: AckRequest) -> AckResponse:
        self._require_active(node_id)
        now = self.clock()
        accepted = ignored = 0
        for digest in request.held:
            a = self.assignments.get((node_id, digest))
            if a is None or a.released_at is not None or a.fill_count == 0:
                ignored += 1
                continue
            if a.acked_at is None:
                a.acked_at = now
            accepted += 1
        for digest in request.released:
            a = self.assignments.get((node_id, digest))
            if a is None or a.released_at is not None:
                ignored += 1
                continue
            a.released_at = now
            accepted += 1
        return AckResponse(accepted=accepted, ignored=ignored)

    def answer(
        self, node_id: str, challenge_id: str, answer: ChallengeAnswer
    ) -> AnswerResponse:
        self._require_active(node_id)
        challenge = self.challenges.get(challenge_id)
        if challenge is None or challenge.node_id != node_id:
            raise CoordinatorError("not_found", "unknown challenge")
        if challenge.outcome != "pending":
            return AnswerResponse(received=True)
        now = self.clock()
        assignment = self.assignments.get((node_id, challenge.drawn.digest))
        challenge.outcome = judge_answer(
            challenge=challenge.drawn,
            data=self.objects[challenge.drawn.digest].data,
            answer=answer,
            issued_at=challenge.issued_at,
            answered_at=now,
            deadline_at=challenge.deadline_at,
            last_filled_at=assignment.last_filled_at if assignment else None,
        )
        challenge.answered_at = now
        return AnswerResponse(received=True)

    def status(self, node_id: str) -> NodeStatusResponse:
        node = self.nodes.get(node_id)
        if node is None:
            raise CoordinatorError("unauthorized", "unknown node")
        now = self.clock()
        held = self._held(node_id, now)
        index, _ = self._window(now)
        summary = None
        schedule = self.slots.get((node_id, index))
        if schedule is not None:
            outcomes: list[Outcome] = []
            for slot in schedule:
                if slot.state == "scheduled":
                    continue
                outcome = self._slot_outcome(slot)
                if outcome == "pending" and slot.challenge_id is not None:
                    # Still inside its deadline: due, but not yet decided.
                    deadline = self.challenges[slot.challenge_id].deadline_at
                    outcome = "missed" if now > deadline else "pending"
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
            node_id=node_id,
            status=node.status,  # type: ignore[arg-type]
            mode=self.settings.mode,
            held_objects=len(held),
            held_bytes=sum(h.size_bytes for h in held),
            current_window=summary,
        )

    def leave(self, node_id: str, request: LeaveRequest) -> None:
        del request
        node = self.nodes.get(node_id)
        if node is None:
            raise CoordinatorError("unauthorized", "unknown node")
        node.status = "retired"
        now = self.clock()
        for a in self.assignments.values():
            if a.node_id == node_id and a.released_at is None:
                a.released_at = now

    # -- ledger ---------------------------------------------------------------

    def _slot_outcome(self, slot: _Slot) -> Outcome:
        """The slot's outcome, leaving an unanswered challenge ``pending``.

        The ledger counts ``pending`` as missed once a window closes; a live
        status read must not, while the deadline is still running.
        """
        if slot.state == "issued" and slot.challenge_id is not None:
            return self.challenges[slot.challenge_id].outcome
        if slot.state == "void":
            return "void"
        return "missed"

    def close_windows(self) -> list[LedgerRow]:
        """Score every window that has closed and whose deadlines have passed."""
        settings = self.settings
        now = self.clock()
        horizon = now - settings.challenge_deadline_seconds - settings.grace_seconds
        closed = sorted(
            {
                index
                for (_, index) in self.slots
                if (index + 1) * settings.window_seconds <= horizon
                and index not in self._scored_windows
            }
        )
        rows: list[LedgerRow] = []
        for index in closed:
            start = float(index * settings.window_seconds)
            end = start + settings.window_seconds
            scores: dict[str, WindowScore] = {}
            for (node_id, slot_index), schedule in sorted(self.slots.items()):
                if slot_index != index:
                    continue
                observations = [
                    Observation(slot.purpose, self._slot_outcome(slot))
                    for slot in schedule
                ]
                holds = [
                    HoldInterval(
                        size_bytes=len(self.objects[a.digest].data),
                        start=a.acked_at,
                        end=_earliest(
                            a.released_at,
                            self.objects[a.digest].revoked_at,
                            a.lease_expires_at if a.lease_expires_at <= now else None,
                        ),
                    )
                    for a in self.assignments.values()
                    if a.node_id == node_id and a.acked_at is not None
                ]
                scores[node_id] = score_window(
                    observations=observations,
                    holds=holds,
                    window_start=start,
                    window_end=end,
                    params=settings.ledger_params(),
                )
            shares = hypothetical_shares(
                {node_id: score.verified_units for node_id, score in scores.items()}
            )
            for node_id, score in scores.items():
                rows.append(
                    LedgerRow(
                        window_index=index,
                        node_id=node_id,
                        operator=self.nodes[node_id].operator,
                        score=score,
                        hypothetical_share=shares[node_id],
                    )
                )
            self._scored_windows.add(index)
        self.ledger.extend(rows)
        return rows


def _earliest(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None
