"""Dry run: the whole lane in one process, against prod-shaped traffic.

A simulated clock drives an in-memory coordinator and a set of nodes through
several ledger windows. It uses real signatures, wire models, placement,
challenges and ledger rules. The run:

- ingests synthetic objects shaped like production's public-content mix,
  and/or real public shares read anonymously from the production API;
- mixes honest nodes with adversaries. Each adversary changes exactly one
  behavior: ``lazy`` holds nothing and re-downloads to answer; ``keeps_revoked``
  ignores revocations; ``tamper`` corrupts answers. ``part_time`` is an honest
  node that is offline half of every window;
- revokes part of the corpus mid-run. Real shares are also rechecked
  anonymously at every window boundary, and an object is revoked once every
  share that published its bytes stopped being public;
- reports the ledger per node, which adversaries were caught and how,
  revocation latency, and the honest false-fail rate.

It needs no deployment, no secret and no admin write, and it pays nothing.
"""

from __future__ import annotations

import base64
import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ditto_edge_protocol.coordinator import Coordinator, LedgerRow
from ditto_edge_protocol.limits import GIB, PROTOCOL
from ditto_edge_protocol.node import EdgeNode, NodeConfig
from ditto_edge_protocol.settings import LaneSettings
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.sources import PublicState, SourceObject
from ditto_edge_protocol.store import MemoryStore
from ditto_edge_protocol.transport import LocalTransport
from ditto_edge_protocol.wire import (
    Challenge,
    ChallengeAnswer,
    ClientInfo,
    NodePlatform,
)

Behavior = Literal["honest", "part_time", "lazy", "keeps_revoked", "tamper"]
ADVERSARIES: tuple[Behavior, ...] = ("lazy", "keeps_revoked", "tamper")


class PublicSource(Protocol):
    def state(self, share_id: str) -> PublicState: ...


class SimClock:
    """Simulated time that never repeats a reading.

    Every read moves time forward by a microsecond, the way real requests take
    time. A re-download made to answer a challenge then always lands after the
    challenge was issued, as it would on Platform.
    """

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1e-6
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _PretendStore(MemoryStore):
    """A lazy node's store: claims everything, keeps nothing, refetches on read."""

    def __init__(self, refetch: Callable[[str], bytes]) -> None:
        super().__init__()
        self._claimed: set[str] = set()
        self._refetch = refetch

    def has(self, digest: str) -> bool:
        return digest in self._claimed

    def read(self, digest: str) -> bytes | None:
        return self._refetch(digest) if digest in self._claimed else None

    def put(self, digest: str, data: bytes) -> None:
        del data
        self._claimed.add(digest)

    def delete(self, digest: str) -> bool:
        if digest in self._claimed:
            self._claimed.discard(digest)
            return True
        return False

    def digests(self) -> set[str]:
        return set(self._claimed)

    def used_bytes(self) -> int:
        return 0


class _KeepsRevokedNode(EdgeNode):
    def _revoke(self, digest: str) -> None:
        # Claims the release but keeps the bytes and answers for them forever.
        self._leases[digest] = sys.maxsize


class _TamperNode(EdgeNode):
    def answer_for(self, challenge: Challenge) -> ChallengeAnswer:
        answer = super().answer_for(challenge)
        if answer.outcome == "proof" and answer.proof_hex is not None:
            flipped = (
                format(int(answer.proof_hex[0], 16) ^ 1, "x") + answer.proof_hex[1:]
            )
            return ChallengeAnswer(outcome="proof", proof_hex=flipped)
        if answer.outcome == "bytes" and answer.data_b64 is not None:
            raw = bytearray(base64.b64decode(answer.data_b64))
            raw[0] ^= 0x01
            return ChallengeAnswer(
                outcome="bytes", data_b64=base64.b64encode(raw).decode()
            )
        return answer


@dataclass
class _SimNode:
    behavior: Behavior
    node: EdgeNode
    key: DeviceKey

    def online(self, now: float, window_seconds: int) -> bool:
        if self.behavior != "part_time":
            return True
        return (now % window_seconds) < window_seconds / 2


@dataclass(frozen=True)
class DryRunConfig:
    honest_nodes: int = 5
    adversaries: tuple[Behavior, ...] = ADVERSARIES
    part_time_nodes: int = 1
    windows: int = 4
    revoke_fraction: float = 0.2
    seed: int = 0
    quota_bytes: int = 2 * GIB
    settings: LaneSettings = field(
        default_factory=lambda: LaneSettings(
            mode="shadow",
            challenges_per_window=12,
            retrieval_share=0.25,
            revoked_share=0.25,
            min_challenges=6,
        )
    )


@dataclass
class _Revocation:
    digest: str
    reason: str
    at: float
    holders: list[str]


def run_dry_run(
    config: DryRunConfig,
    objects: Sequence[SourceObject],
    *,
    public_source: PublicSource | None = None,
) -> dict[str, Any]:
    settings = config.settings
    if settings.mode != "shadow":
        raise ValueError("a dry run needs a shadow lane")
    rng = random.Random(config.seed)
    start = float(1_763_000_000 // settings.window_seconds * settings.window_seconds)
    clock = SimClock(start)
    coordinator = Coordinator(settings, clock=clock, rng=rng, nonce=rng.randbytes)

    # Several shares with identical bytes ingest as one digest; keep them all.
    labels: dict[str, list[str]] = {}
    for obj in objects:
        digest = coordinator.ingest(obj.data, obj.media_type, source=obj.label)
        names = labels.setdefault(digest, [])
        if obj.label not in names:
            names.append(obj.label)

    honest: list[Behavior] = ["honest"] * config.honest_nodes
    part_time: list[Behavior] = ["part_time"] * config.part_time_nodes
    behaviors = honest + part_time + list(config.adversaries)
    sims: list[_SimNode] = []
    for index, behavior in enumerate(behaviors):
        key = DeviceKey.from_seed(rng.randbytes(32))
        transport = LocalTransport(coordinator=coordinator, key=key)
        node_config = NodeConfig(
            public_key_hex=key.public_key_hex,
            quota_bytes=config.quota_bytes,
            client=ClientInfo(name="dry-run", version=f"{behavior}-{index}"),
            platform=NodePlatform(os="simulated"),
        )
        store = _PretendStore(transport.fill) if behavior == "lazy" else MemoryStore()
        node_class = {"keeps_revoked": _KeepsRevokedNode, "tamper": _TamperNode}.get(
            behavior, EdgeNode
        )
        node = node_class(
            transport=transport, store=store, config=node_config, clock=clock
        )
        node.enroll()
        sims.append(_SimNode(behavior=behavior, node=node, key=key))

    tick = settings.poll_interval_seconds
    ticks_per_window = settings.window_seconds // tick
    if ticks_per_window == 0:
        raise ValueError("a dry run needs at least one poll interval per window")
    total_ticks = config.windows * ticks_per_window
    revoke_tick = total_ticks // 2
    revocations: list[_Revocation] = []

    def revoke(digest: str, reason: str) -> None:
        holders = [
            a.node_id
            for a in coordinator.assignments.values()
            if a.digest == digest and a.acked_at is not None and a.released_at is None
        ]
        if coordinator.revoke(digest, reason):
            revocations.append(_Revocation(digest, reason, clock(), holders))

    for step_index in range(total_ticks):
        now = clock()
        for sim in sims:
            if sim.online(now, settings.window_seconds):
                sim.node.step()
        # Unpublishes land between polls, so release latency is measured from
        # mid-tick to the next poll that acknowledges the deletion.
        clock.advance(tick / 2)
        if step_index == revoke_tick and config.revoke_fraction > 0:
            active = sorted(
                d for d, o in coordinator.objects.items() if o.revoked_at is None
            )
            count = max(1, int(len(active) * config.revoke_fraction)) if active else 0
            targets = set(rng.sample(active, count))
            # Red team: make sure every keeps_revoked adversary holds at least one
            # revoked object, or the attack it stands for is never exercised.
            for sim in sims:
                if sim.behavior == "keeps_revoked":
                    held = sorted(sim.node.store.digests() & set(active))
                    if held and not targets & set(held):
                        targets.add(rng.choice(held))
            for digest in sorted(targets):
                revoke(digest, "dry-run revocation")
        if public_source is not None and step_index % ticks_per_window == 0:
            for digest, names in labels.items():
                shares = [
                    name.removeprefix("share:")
                    for name in names
                    if name.startswith("share:")
                ]
                # The bytes stay public while any share still publishes them,
                # and a revoked digest never comes back, so revoke only once
                # every share for this digest reads as unpublished.
                if (
                    shares
                    and coordinator.objects[digest].revoked_at is None
                    and all(
                        public_source.state(share) == "unpublished" for share in shares
                    )
                ):
                    revoke(digest, "source unpublished")
        clock.advance(tick / 2)
        coordinator.close_windows()
    clock.advance(settings.window_seconds + settings.challenge_deadline_seconds + 60)
    coordinator.close_windows()
    return _report(config, coordinator, sims, revocations, labels)


def _report(
    config: DryRunConfig,
    coordinator: Coordinator,
    sims: list[_SimNode],
    revocations: list[_Revocation],
    labels: dict[str, list[str]],
) -> dict[str, Any]:
    by_node: dict[str, list[LedgerRow]] = {}
    for row in coordinator.ledger:
        by_node.setdefault(row.node_id, []).append(row)

    nodes: list[dict[str, Any]] = []
    honest_windows = honest_failures = 0
    caught: dict[str, bool] = {}
    for sim in sims:
        node_id = sim.key.node_id
        rows = sorted(by_node.get(node_id, []), key=lambda r: r.window_index)
        reasons = sorted({reason for row in rows for reason in row.score.reasons})
        first_caught = next(
            (
                row.window_index
                for row in rows
                if {
                    "integrity_failure",
                    "revocation_violation",
                    "availability_below_floor",
                }
                & set(row.score.reasons)
            ),
            None,
        )
        if sim.behavior == "honest":
            # The first window includes enrollment and initial fills, and the
            # last one ends with the run: slots due after its final poll are
            # never asked. Judge the windows in between.
            for row in rows[1:-1]:
                honest_windows += 1
                honest_failures += not row.score.eligible
        if sim.behavior in ADVERSARIES:
            # Several instances of one behavior pass only if every one is caught.
            caught[sim.behavior] = caught.get(sim.behavior, True) and (
                first_caught is not None
            )
        nodes.append(
            {
                "node_id": node_id,
                "behavior": sim.behavior,
                "verified_units": round(sum(r.score.verified_units for r in rows), 9),
                "eligible_windows": sum(r.score.eligible for r in rows),
                "windows": [
                    {
                        "window_index": r.window_index,
                        "eligible": r.score.eligible,
                        "availability": r.score.availability,
                        "held_gib_hours": r.score.held_gib_hours,
                        "verified_units": r.score.verified_units,
                        "hypothetical_share": r.hypothetical_share,
                        "scheduled": r.score.scheduled,
                        "passed": r.score.passed,
                        "missed": r.score.missed,
                        "refilled": r.score.refilled,
                        "failed": r.score.failed,
                        "revocation_checks": r.score.revocation_checks,
                        "revocation_violations": r.score.revocation_violations,
                        "reasons": list(r.score.reasons),
                        "weight_eligible": r.weight_eligible,
                    }
                    for r in rows
                ],
                "first_flagged_window": first_caught,
                "reasons": reasons,
            }
        )

    # The release bound applies to nodes that are online. An offline holder
    # releases on its next poll; until then its lease lapses, so nothing would
    # route to it. Its latency is reported, not judged.
    behavior_of = {s.key.node_id: s.behavior for s in sims}
    latencies: list[float] = []
    offline_latencies: list[float] = []
    late_holders = 0
    for revocation in revocations:
        for holder in revocation.holders:
            behavior = behavior_of.get(holder)
            if behavior not in ("honest", "part_time"):
                continue
            assignment = coordinator.assignments.get((holder, revocation.digest))
            released = assignment.released_at if assignment else None
            if released is None:
                late_holders += 1
            elif behavior == "honest":
                latencies.append(released - revocation.at)
            else:
                offline_latencies.append(released - revocation.at)
    false_fail_rate = (
        round(honest_failures / honest_windows, 9) if honest_windows else 0.0
    )
    bound = config.settings.lease_seconds + config.settings.poll_interval_seconds
    honest_units = [n["verified_units"] for n in nodes if n["behavior"] == "honest"]
    part_time_units = [
        n["verified_units"] for n in nodes if n["behavior"] == "part_time"
    ]
    return {
        "protocol": PROTOCOL,
        "payouts": "none",
        "weight_eligible": False,
        "config": {
            "honest_nodes": config.honest_nodes,
            "part_time_nodes": config.part_time_nodes,
            "adversaries": list(config.adversaries),
            "windows": config.windows,
            "revoke_fraction": config.revoke_fraction,
            "seed": config.seed,
            "settings": {
                k: getattr(config.settings, k)
                for k in config.settings.__dataclass_fields__
            },
        },
        "objects": {
            "ingested": len(coordinator.objects),
            "bytes": sum(len(o.data) for o in coordinator.objects.values()),
            "public_shares": sum(
                name.startswith("share:") for names in labels.values() for name in names
            ),
            "revoked": len(revocations),
            "revoked_unpublished": sum(
                r.reason == "source unpublished" for r in revocations
            ),
        },
        "nodes": nodes,
        "revocation": {
            "events": len(revocations),
            "honest_holders": len(latencies) + len(offline_latencies) + late_holders,
            "honest_not_released": late_holders,
            "max_release_seconds": max(latencies) if latencies else None,
            "max_part_time_release_seconds": (
                max(offline_latencies) if offline_latencies else None
            ),
            "bound_seconds": bound,
        },
        "verdict": {
            "honest_false_fail_rate": false_fail_rate,
            "adversaries_caught": caught,
            # None when there is no pair to compare, rather than a vacuous True.
            "part_time_paid_less": (
                all(units < min(honest_units) for units in part_time_units)
                if honest_units and part_time_units
                else None
            ),
            # None when no honest holder released a revocation: nothing to judge.
            "revocation_within_bound": (
                late_holders == 0 and all(latency <= bound for latency in latencies)
                if latencies or late_holders
                else None
            ),
            # A pass needs both checks exercised: with no adversary caught or
            # no release measured, it would be vacuous.
            "pass": false_fail_rate == 0.0
            and bool(caught)
            and all(caught.values())
            and bool(latencies)
            and late_holders == 0
            and all(latency <= bound for latency in latencies),
        },
    }
