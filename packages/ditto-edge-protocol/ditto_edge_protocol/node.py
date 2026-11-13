"""The reference edge/v1 node: the behavior every node implementation copies.

One :meth:`EdgeNode.step` is one poll. It runs in this order, which the
Desktop helper follows exactly:

1. **Poll.** Report free quota; receive limits, assignments (each with a
   renewed lease), revocations and due challenges.
2. **Revoke.** Delete every revoked object, remember the digest, and report it
   released. From then on, every challenge for it is refused with ``revoked``.
3. **Answer.** Answer each challenge from bytes already on disk. A node never
   downloads to answer: an object that is missing is refused as ``not_held``.
4. **Fill.** Download each listed assignment the node does not hold yet. Check
   its SHA-256 and size before storing it.
5. **Expire.** Delete objects that are no longer listed once their lease has
   lapsed.
6. **Acknowledge.** Report what was filled and verified, and what was
   released. Acknowledging is idempotent, so after a restart the node simply
   re-acknowledges everything listed that it still holds.

The node holds no secret except its device key, sees no URL or owner, and
never serves anyone but Platform.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ditto_edge_protocol.limits import MAX_ACK_ITEMS, PROTOCOL
from ditto_edge_protocol.proofs import ProofError, digest_of, possession_proof
from ditto_edge_protocol.store import ObjectStore
from ditto_edge_protocol.transport import TransportError
from ditto_edge_protocol.wire import (
    AckRequest,
    AckResponse,
    AnswerResponse,
    Assignment,
    Challenge,
    ChallengeAnswer,
    ClientInfo,
    EnrollRequest,
    EnrollResponse,
    HotkeyBinding,
    LaneLimits,
    LeaveRequest,
    NodePlatform,
    NodeStatusResponse,
    PollRequest,
    PollResponse,
)

logger = logging.getLogger(__name__)


class Transport(Protocol):
    """How a node reaches Platform. Implementations sign every request."""

    def enroll(self, request: EnrollRequest) -> EnrollResponse: ...
    def poll(self, request: PollRequest) -> PollResponse: ...
    def fill(self, digest: str) -> bytes: ...
    def ack(self, request: AckRequest) -> AckResponse: ...
    def answer(self, challenge_id: str, answer: ChallengeAnswer) -> AnswerResponse: ...
    def status(self) -> NodeStatusResponse: ...
    def leave(self, request: LeaveRequest) -> None: ...


@dataclass(frozen=True)
class NodeConfig:
    public_key_hex: str
    quota_bytes: int
    client: ClientInfo
    platform: NodePlatform = NodePlatform()
    hotkey_binding: HotkeyBinding | None = None


@dataclass
class StepReport:
    filled: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    answered: int = 0
    refused: int = 0
    rejected_fills: int = 0
    failed_answers: int = 0
    """Answers Platform did not take. It re-sends them until their deadline."""
    next_poll_seconds: int = 30


def _ends_step(exc: TransportError) -> bool:
    """Errors that end a step: no network, refused auth, or the lane is off.

    Any other failure is about one object or one answer. The step goes on:
    Platform re-sends a pending challenge, and the next poll lists the object
    again.
    """
    return exc.status in (0, 401, 403) or exc.lane_off


class EdgeNode:
    def __init__(
        self,
        *,
        transport: Transport,
        store: ObjectStore,
        config: NodeConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self.store = store
        self.config = config
        self.clock = clock
        self.limits: LaneLimits | None = None
        self.node_id: str | None = None
        self._leases: dict[str, int] = {}
        self._revoked: set[str] = set()
        self._acked: set[str] = set()

    def enroll(self) -> EnrollResponse:
        response = self.transport.enroll(
            EnrollRequest(
                protocol=PROTOCOL,
                public_key_hex=self.config.public_key_hex,
                quota_bytes=self.config.quota_bytes,
                client=self.config.client,
                platform=self.config.platform,
                hotkey_binding=self.config.hotkey_binding,
            )
        )
        self.node_id = response.node_id
        self.limits = response.limits
        return response

    def free_bytes(self) -> int:
        quota = self.config.quota_bytes
        if self.limits is not None:
            quota = min(quota, self.limits.node_quota_cap_bytes)
        return max(0, quota - self.store.used_bytes())

    def step(self) -> StepReport:
        report = StepReport()
        try:
            response = self.transport.poll(
                PollRequest(protocol=PROTOCOL, free_bytes=self.free_bytes())
            )
        except TransportError as exc:
            if exc.lane_off:
                # The operator's kill switch. Revocations cannot reach this node
                # while the lane is off, so it holds nothing until it reopens.
                self._drop_cache()
            raise
        self.limits = response.limits
        report.next_poll_seconds = response.next_poll_seconds

        for digest in response.revocations:
            self._revoke(digest)
            report.released.append(digest)

        for challenge in response.challenges:
            answer = self.answer_for(challenge)
            report.answered += 1
            if answer.outcome == "refused":
                report.refused += 1
            try:
                self.transport.answer(challenge.challenge_id, answer)
            except TransportError as exc:
                if _ends_step(exc):
                    raise
                report.failed_answers += 1

        listed: set[str] = set()
        for assignment in response.assignments:
            listed.add(assignment.digest)
            if assignment.digest in self._revoked:
                continue
            previous = self._leases.get(assignment.digest)
            if previous is not None and previous <= response.server_time:
                # The lease lapsed, by Platform's clock, before this poll. Platform
                # closed that span and opened a new one, which needs a fresh
                # acknowledgement even though the bytes never left the disk.
                self._acked.discard(assignment.digest)
            self._leases[assignment.digest] = assignment.lease_expires_at
            if not self.store.has(assignment.digest):
                try:
                    filled = self._fill(assignment)
                except TransportError as exc:
                    if _ends_step(exc):
                        raise
                    filled = False  # one object Platform cannot serve now
                if filled:
                    report.filled.append(assignment.digest)
                else:
                    report.rejected_fills += 1

        # Leases are Platform's, so they expire by Platform's clock.
        now = response.server_time
        for digest in self.store.digests() - listed:
            if self._leases.get(digest, 0) <= now:
                self.store.delete(digest)
                self._leases.pop(digest, None)
                self._acked.discard(digest)
                report.released.append(digest)

        held = sorted(
            digest
            for digest in listed
            if digest not in self._acked and self.store.has(digest)
        )
        released = sorted(set(report.released))
        # One acknowledgement carries at most MAX_ACK_ITEMS of each list.
        for start in range(0, max(len(held), len(released)), MAX_ACK_ITEMS):
            held_part = held[start : start + MAX_ACK_ITEMS]
            released_part = released[start : start + MAX_ACK_ITEMS]
            self.transport.ack(AckRequest(held=held_part, released=released_part))
            self._acked.update(held_part)
        return report

    def _drop_cache(self) -> None:
        for digest in self.store.digests():
            self.store.delete(digest)
        self._leases.clear()
        self._acked.clear()

    def _revoke(self, digest: str) -> None:
        self.store.delete(digest)
        self._revoked.add(digest)
        self._leases.pop(digest, None)
        self._acked.discard(digest)

    def _fill(self, assignment: Assignment) -> bool:
        if (
            self.limits is not None
            and assignment.size_bytes > self.limits.max_object_bytes
        ):
            return False
        if assignment.size_bytes > self.free_bytes():
            return False
        data = self.transport.fill(assignment.digest)
        if len(data) != assignment.size_bytes or digest_of(data) != assignment.digest:
            logger.warning(
                "filled bytes for %s did not verify; dropped", assignment.digest
            )
            return False
        self.store.put(assignment.digest, data)
        return True

    def answer_for(self, challenge: Challenge) -> ChallengeAnswer:
        """Answer from bytes on disk only; never download to answer."""
        if challenge.digest in self._revoked:
            return ChallengeAnswer(outcome="refused", refusal="revoked")
        data = self.store.read(challenge.digest)
        if data is None:
            return ChallengeAnswer(outcome="refused", refusal="not_held")
        try:
            if challenge.kind == "possession" and challenge.nonce_hex is not None:
                return ChallengeAnswer(
                    outcome="proof",
                    proof_hex=possession_proof(
                        nonce_hex=challenge.nonce_hex,
                        data=data,
                        offset=challenge.offset,
                        length=challenge.length,
                    ),
                )
            if challenge.kind == "retrieval":
                window = data[challenge.offset : challenge.offset + challenge.length]
                if len(window) == challenge.length:
                    return ChallengeAnswer(
                        outcome="bytes", data_b64=base64.b64encode(window).decode()
                    )
        except ProofError:
            pass
        return ChallengeAnswer(outcome="refused", refusal="busy")

    def leave(self, reason: str = "") -> None:
        """Tell Platform to reassign everything, then delete the cache."""
        self.transport.leave(LeaveRequest(reason=reason))
        for digest in self.store.digests():
            self.store.delete(digest)
        self._leases.clear()
        self._acked.clear()

    def run(self, stop: threading.Event, *, max_backoff_seconds: float = 300.0) -> None:
        """Enroll, then poll until ``stop`` is set, backing off on errors."""
        backoff = 5.0
        while not stop.is_set():
            try:
                if self.node_id is None:
                    self.enroll()
                report = self.step()
                backoff = 5.0
                wait = float(report.next_poll_seconds)
            except Exception:  # a long-running node must survive any one poll
                logger.warning("edge node step failed; retrying", exc_info=True)
                wait = backoff
                backoff = min(backoff * 2, max_backoff_seconds)
            stop.wait(wait)
