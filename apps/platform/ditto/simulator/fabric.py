"""Deterministic fabrication helpers for simulator scenarios.

A :class:`Fabric` derives every identifier (hotkeys, digests, signatures,
UUIDs) from ``(seed, name)`` via SHA-256, so re-running a scenario with the
same ``--seed`` produces byte-identical rows regardless of call order. All
timestamps are computed relative to one fixed ``now`` captured at run start.

Builder coroutines insert *consistent object graphs* — every row an agent in
a given lifecycle stage is expected to have (dataset pin, payment, screening
attempt, tickets, scores, audit entries) — using the real ORM models and the
real :func:`ditto.db.queries.audit.append_audit_entry` so the public audit
chain verifies. Callers own the transaction (``async with session.begin()``).

Fabricated signatures are hex noise of the correct length: they satisfy DB
CHECK constraints and render on public endpoints, but are not real sr25519
signatures. Local-dev only.
"""

from __future__ import annotations

import hashlib
import random
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.onchain_seed import derive_seed
from ditto.db.models import (
    Agent,
    AthReview,
    BenchmarkDataset,
    EvaluationPayment,
    Score,
    ScreenedImageUpload,
    ScreenerHeartbeat,
    ScreeningAttempt,
    ScreeningQuarantine,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.audit import EVENT_FINALIZED, EVENT_SCORE, append_audit_entry
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

# Deterministic UUIDv5 namespace for the simulator (itself derived once).
_UUID_NAMESPACE = uuid_mod.UUID("dd110000-5117-4a70-8000-000000000118")

# Base58 alphabet used by SS58 addresses (no 0, O, I, l).
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Bench version the simulator seeds under.
#
# This used to mirror ``benchmark_rollout.DEFAULT_BENCH_VERSION`` (2), which is
# the version assumed when no rollout has ever activated. That is a statement
# about the very beginning of the subnet's history, and it stopped being a
# usable default the moment v2-v6 were retired: every row the simulator wrote
# would now be refused by ``scores_bench_version_floor`` and the
# ``validator_tickets`` floor trigger, so seeding a local stack would fail
# outright.
#
# Pinned to the floor instead. The simulator exists to populate a database that
# behaves like production, and production cannot hold a sub-v7 score.
DEFAULT_BENCH_VERSION = MIN_SCOREABLE_BENCH_VERSION

# Tables the simulator owns, children before parents. TRUNCATE ... CASCADE
# makes the order advisory, but keeping it dependency-safe documents intent.
SIMULATOR_TABLES: tuple[str, ...] = (
    "score_audit_log",
    "validator_request_nonces",
    "validator_retry_recoveries",
    "validator_tickets",
    "scores",
    "benchmark_rollout_audit",
    "benchmark_rollout_members",
    "benchmark_rollouts",
    "benchmark_datasets",
    "ath_review_actions",
    "ath_reviews",
    "screening_disputes",
    "screening_quarantine_resolutions",
    "screening_quarantines",
    "screened_image_uploads",
    "screening_attempts",
    "evaluation_payments",
    "validator_heartbeats",
    "screener_heartbeats",
    "banned_hotkeys",
    "agents",
)

_TOOL_CATEGORIES = ("web_search", "calendar", "email", "file_io", "code_exec")
_MEMORY_CATEGORIES = ("recall_fact", "temporal_reasoning", "preference", "multi_hop")
_CASE_NOTES = (
    "1 extra tool call",
    "missing required argument",
    "capped: self-report untrusted",
    "stale memory recalled",
)

_ADJECTIVES = (
    "amber", "brisk", "cobalt", "drift", "ember", "fable", "glint", "hazel",
    "iris", "juniper", "kestrel", "lunar", "mica", "nimbus", "onyx", "pico",
)  # fmt: skip
_NOUNS = (
    "anchor", "beacon", "cairn", "dynamo", "echo", "forge", "gantry", "helix",
    "isotope", "jetty", "krill", "lattice", "meridian", "nexus", "orbit", "prism",
)  # fmt: skip


def _digest(seed: int, name: str) -> bytes:
    """32 deterministic bytes for ``(seed, name)``."""
    return hashlib.sha256(f"ditto-simulator:{seed}:{name}".encode()).digest()


@dataclass(frozen=True)
class FabricConfig:
    """Immutable inputs that make a fabrication run reproducible."""

    seed: int
    """Master seed; every derived identifier folds it in."""

    now: datetime
    """The fixed reference instant (tz-aware UTC) all timestamps offset from."""

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("FabricConfig.now must be timezone-aware (UTC)")


async def wipe_all(session: AsyncSession) -> None:
    """TRUNCATE every simulator-owned table (CASCADE), inside the caller's txn.

    Leaves ``alembic_version`` untouched. Identity sequences restart so the
    audit-log ``seq`` starts at 1 on every fresh scenario.
    """
    tables = ", ".join(SIMULATOR_TABLES)
    await session.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


class Fabric:
    """Deterministic identifier + object-graph factory for one simulator run."""

    def __init__(self, config: FabricConfig) -> None:
        self.config = config
        self.rng = random.Random(config.seed)
        """Scenario-level RNG for choices that need no per-name stability."""

    @property
    def now(self) -> datetime:
        """The run's fixed reference instant."""
        return self.config.now

    # ── identifiers ──────────────────────────────────────────────────────

    def name_rng(self, name: str) -> random.Random:
        """A fresh RNG deterministically seeded from ``(seed, name)``."""
        return random.Random(_digest(self.config.seed, name))

    def ss58_hotkey(self, name: str) -> str:
        """Plausible SS58-shaped address: ``5`` + 47 base58 chars, per name."""
        rng = self.name_rng(f"ss58:{name}")
        return "5" + "".join(rng.choice(_BASE58_ALPHABET) for _ in range(47))

    def hex_digest(self, name: str) -> str:
        """64 lowercase hex chars (sha256-shaped), deterministic per name."""
        return _digest(self.config.seed, f"hex:{name}").hex()

    def signature(self, name: str) -> str:
        """128 lowercase hex chars (sr25519-signature-shaped), per name."""
        return self.hex_digest(f"sig-a:{name}") + self.hex_digest(f"sig-b:{name}")

    def uuid(self, name: str) -> UUID:
        """Deterministic UUIDv5 for ``(seed, name)``."""
        return uuid_mod.uuid5(_UUID_NAMESPACE, f"{self.config.seed}:{name}")

    def screened_image_id(self, name: str) -> str:
        """71-char Docker content ID: ``sha256:`` + 64 hex."""
        return "sha256:" + self.hex_digest(f"image:{name}")

    def block_hash(self, name: str) -> str:
        """``0x``-prefixed 64-hex block hash, deterministic per name."""
        return "0x" + self.hex_digest(f"block:{name}")

    def miner_name(self, index: int) -> str:
        """Deterministic human-ish miner label, unique per index."""
        rng = self.name_rng(f"miner-name:{index}")
        return f"{rng.choice(_ADJECTIVES)}-{rng.choice(_NOUNS)}-{index:02d}"

    def agent_name(self, index: int) -> str:
        """Deterministic agent name, unique per index."""
        rng = self.name_rng(f"agent-name:{index}")
        return f"{rng.choice(_NOUNS)}-agent-{index:02d}"

    def system_metrics(
        self,
        name: str,
        *,
        cpu_percent: int | None = None,
        memory_percent: int | None = None,
        disk_percent: int | None = None,
        docker_status: str = "healthy",
        running_containers: int = 6,
        unhealthy_containers: int = 0,
    ) -> dict[str, Any]:
        """A telemetry dict that passes the strict public ``SystemMetrics`` model.

        The public endpoints re-validate stored telemetry (ints, multiples of
        5, nested docker health) and silently drop anything else, so defaults
        here must satisfy that allowlist or fleet health renders as "unknown".
        """
        rng = self.name_rng(f"sysmetrics:{name}")

        def pick(low: int, high: int) -> int:
            return rng.randrange(low, high + 5, 5)

        return {
            "collected_at": int(self.now.timestamp()),
            "cpu_percent": cpu_percent if cpu_percent is not None else pick(10, 60),
            "memory_percent": (
                memory_percent if memory_percent is not None else pick(25, 70)
            ),
            "disk_percent": disk_percent if disk_percent is not None else pick(15, 55),
            "docker": {
                "status": docker_status,
                "running_containers": running_containers,
                "unhealthy_containers": unhealthy_containers,
            },
        }

    # ── timestamps (all relative to the fixed now) ───────────────────────

    def minutes_ago(self, minutes: float) -> datetime:
        return self.now - timedelta(minutes=minutes)

    def hours_ago(self, hours: float) -> datetime:
        return self.now - timedelta(hours=hours)

    def days_ago(self, days: float) -> datetime:
        return self.now - timedelta(days=days)

    def minutes_from_now(self, minutes: float) -> datetime:
        return self.now + timedelta(minutes=minutes)

    # ── low-level row builders ───────────────────────────────────────────

    def _new_agent(
        self,
        *,
        index: int,
        status: AgentStatus,
        miner_hotkey: str | None,
        name: str | None,
        created_at: datetime | None,
        version: int = 1,
    ) -> Agent:
        name = name if name is not None else self.agent_name(index)
        return Agent(
            agent_id=self.uuid(f"agent:{index}:{name}"),
            miner_hotkey=miner_hotkey or self.ss58_hotkey(f"miner:{index}"),
            name=name,
            version=version,
            sha256=self.hex_digest(f"tarball:{index}:{name}"),
            size_bytes=200_000 + index * 1_337,
            normalized_source_hash=self.hex_digest(f"nsh:{index}:{name}"),
            status=status,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=created_at or self.hours_ago(24 + index),
        )

    def _payment(self, agent: Agent) -> EvaluationPayment:
        """The 1:1 payment row every agent is expected to carry."""
        key = f"payment:{agent.agent_id}"
        return EvaluationPayment(
            block_hash=self.block_hash(key),
            extrinsic_index=1,
            agent_id=agent.agent_id,
            miner_hotkey=agent.miner_hotkey,
            miner_coldkey=self.ss58_hotkey(f"coldkey:{agent.miner_hotkey}"),
            amount_rao=17_500_000,
            dest_address=self.ss58_hotkey("platform-payment-dest"),
            timestamp=agent.created_at - timedelta(minutes=2),
            created_at=agent.created_at,
        )

    def _pin_dataset(
        self, agent: Agent, *, bench_version: int, run_size: str
    ) -> BenchmarkDataset:
        """Set the agent's dataset_* columns + the matching benchmark_datasets row.

        The seed is derived from the fabricated block hash via the real
        :func:`derive_seed`, so ``GET /agent/{id}/dataset`` provenance recomputes.
        """
        block_hash = self.block_hash(f"seed-block:{agent.agent_id}")
        seed = derive_seed(block_hash, agent.agent_id)
        agent.dataset_seed = seed
        agent.dataset_sha256 = self.hex_digest(f"dataset:{agent.agent_id}")
        agent.dataset_run_size = run_size
        agent.dataset_seed_block = 5_000_000 + (seed % 100_000)
        agent.dataset_seed_block_hash = block_hash
        return BenchmarkDataset(
            agent_id=agent.agent_id,
            bench_version=bench_version,
            seed=seed,
            sha256=agent.dataset_sha256,
            run_size=run_size,
            seed_block=agent.dataset_seed_block,
            seed_block_hash=block_hash,
            created_at=agent.created_at + timedelta(minutes=30),
        )

    async def _passed_attempt(
        self,
        session: AsyncSession,
        agent: Agent,
        *,
        screener_hotkey: str,
        with_image: bool,
    ) -> ScreeningAttempt:
        """Insert a finished ``passed`` attempt (+ verified image upload).

        The attempt is flushed before the image upload row: without ORM
        relationships the unit of work does not order inserts across mappers,
        and ``screened_image_uploads.attempt_id`` is FK-bound.
        """
        started = agent.created_at + timedelta(minutes=5)
        finished = started + timedelta(minutes=20)
        attempt = ScreeningAttempt(
            attempt_id=self.uuid(f"attempt:{agent.agent_id}"),
            agent_id=agent.agent_id,
            screener_hotkey=screener_hotkey,
            policy_version=SCREENING_POLICY_VERSION,
            status="passed",
            started_at=started,
            deadline=started + timedelta(hours=1),
            finished_at=finished,
        )
        session.add(attempt)
        await session.flush()
        if not with_image:
            return attempt
        key = f"screened-image:{agent.agent_id}"
        upload = ScreenedImageUpload(
            image_upload_id=self.uuid(f"image-upload:{agent.agent_id}"),
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            screener_hotkey=screener_hotkey,
            storage_upload_id=self.hex_digest(f"storage:{key}")[:32],
            sha256=self.hex_digest(key),
            size_bytes=350_000_000,
            image_id=self.screened_image_id(key),
            image_ref=f"ditto-screened/{agent.agent_id}:verified",
            status="verified",
            created_at=finished - timedelta(minutes=5),
            expires_at=finished + timedelta(hours=1),
            verified_at=finished,
        )
        agent.screened_image_sha256 = upload.sha256
        agent.screened_image_size_bytes = upload.size_bytes
        agent.screened_image_id = upload.image_id
        agent.screened_image_ref = upload.image_ref
        agent.screened_image_upload_id = upload.image_upload_id
        agent.screened_image_verified_at = upload.verified_at
        session.add(upload)
        await session.flush()
        return attempt

    def _score_row(
        self,
        agent: Agent,
        *,
        validator_hotkey: str,
        composite: float,
        bench_version: int,
        n_cases: int,
        generated_at: datetime,
        ticket_deadline: datetime,
        median_ms: int | None = None,
        rich_details: bool = False,
    ) -> Score:
        rng = self.name_rng(f"score:{agent.agent_id}:{validator_hotkey}")
        seed = agent.dataset_seed
        if seed is None:
            raise ValueError("score fabrication requires a pinned dataset seed")
        resolved_median_ms = (
            median_ms if median_ms is not None else rng.randint(900, 2_400)
        )
        details: dict[str, Any] = {
            "bench_version": bench_version,
            "ticket_deadline": ticket_deadline.astimezone(UTC).isoformat(),
            "transcript_sha256": self.hex_digest(
                f"transcript:{agent.agent_id}:{validator_hotkey}"
            ),
        }
        if rich_details:
            details.update(
                self._run_details(
                    rng,
                    agent=agent,
                    composite=composite,
                    n_cases=n_cases,
                    median_ms=resolved_median_ms,
                )
            )
        return Score(
            agent_id=agent.agent_id,
            validator_hotkey=validator_hotkey,
            bench_version=bench_version,
            run_id=str(self.uuid(f"run:{agent.agent_id}:{validator_hotkey}")),
            signature=self.signature(f"score:{agent.agent_id}:{validator_hotkey}"),
            seed=seed,
            composite=round(min(1.0, max(0.0, composite)), 4),
            tool_mean=round(
                min(1.0, max(0.0, composite + rng.uniform(-0.03, 0.03))), 4
            ),
            memory_mean=round(
                min(1.0, max(0.0, composite + rng.uniform(-0.03, 0.03))), 4
            ),
            median_ms=resolved_median_ms,
            n=n_cases,
            details=details,
            generated_at=generated_at,
            created_at=generated_at,
            updated_at=generated_at,
        )

    def _run_details(
        self,
        rng: random.Random,
        *,
        agent: Agent,
        composite: float,
        n_cases: int,
        median_ms: int,
    ) -> dict[str, Any]:
        """Plausible run telemetry mirroring what real validators report.

        Everything here feeds the public leaderboard's safe-subset extractors:
        ``per_case`` (redacted to :class:`PublicCaseResult` — no answer key to
        omit because none is fabricated), ``per_category``, ``models``,
        ``tokens``, ``dataset_sha256``, the paraphrase / lexical-gap integrity
        counters, and the advisory calibration pair. Case pass-rate tracks
        ``composite`` so the per-case view is consistent with the score.
        """
        target = min(0.98, max(0.05, composite))
        per_case: list[dict[str, Any]] = []
        for _ in range(n_cases):
            kind = "tool" if rng.random() < 0.55 else "memory"
            category = rng.choice(
                _TOOL_CATEGORIES if kind == "tool" else _MEMORY_CATEGORIES
            )
            passed = rng.random() < target
            if passed:
                score = 1.0
            elif rng.random() < 0.3:
                score = round(rng.uniform(0.2, 0.6), 2)
            else:
                score = 0.0
            case: dict[str, Any] = {
                "category": category,
                "kind": kind,
                "score": score,
                "correct": passed,
                "latency_ms": max(120, int(rng.gauss(median_ms, median_ms * 0.35))),
            }
            if not passed and rng.random() < 0.4:
                case["notes"] = [rng.choice(_CASE_NOTES)]
            per_case.append(case)
        by_category: dict[str, list[float]] = {}
        for case in per_case:
            by_category.setdefault(case["category"], []).append(case["score"])
        per_category = [
            {
                "category": category,
                "count": len(scores),
                "mean": round(sum(scores) / len(scores), 4),
            }
            for category, scores in sorted(by_category.items())
        ]
        tool_cases = sum(1 for case in per_case if case["kind"] == "tool")
        memory_cases = n_cases - tool_cases
        attempted = memory_cases
        applied = int(attempted * rng.uniform(0.82, 0.98))
        return {
            "dataset_sha256": agent.dataset_sha256,
            "models": {
                "generator": "gemini-2.5-pro",
                "judge": "claude-sonnet-4-5",
                "judge_audit": "gpt-5-mini",
            },
            "tokens": rng.randint(1_500_000, 8_000_000),
            "per_case": per_case,
            "per_category": per_category,
            "paraphrase": {
                "applied": applied,
                "attempted": attempted,
                "fallback": attempted - applied,
            },
            "lexical_gap": {
                "rewritten": int(memory_cases * rng.uniform(0.6, 0.9)),
                "questions": memory_cases,
                "mean_before": round(rng.uniform(0.52, 0.68), 3),
                "mean_after": round(rng.uniform(0.18, 0.34), 3),
            },
            "capped_tool_cases": rng.randint(0, max(1, tool_cases // 20)),
            "seeding_waves": 3,
            "calibration_brier": round(rng.uniform(0.08, 0.26), 3),
            "calibration_n": rng.randint(max(2, n_cases // 4), n_cases),
        }

    # ── heartbeat builders ───────────────────────────────────────────────

    async def validator_heartbeat(
        self,
        session: AsyncSession,
        *,
        name: str,
        state: str = "polling",
        software_version: str = "0.9.0",
        protocol_version: int = 8,
        seen_ago_seconds: float = 30.0,
        active_agent_id: UUID | None = None,
        benchmark_progress: dict[str, Any] | None = None,
        system_metrics: dict[str, Any] | None = None,
        capabilities: dict[str, Any] | None = None,
        stack: dict[str, Any] | None = None,
    ) -> ValidatorHeartbeat:
        """Insert one constraint-satisfying validator heartbeat row.

        ``name`` derives the hotkey (``fabric.ss58_hotkey(f"validator:{name}")``)
        so tickets/scores fabricated with the same name line up. Defaults yield
        an *online, healthy, available* validator on the public fleet view.
        """
        seen_at = self.now - timedelta(seconds=seen_ago_seconds)
        if system_metrics is None:
            system_metrics = self.system_metrics(f"validator:{name}")
        row = ValidatorHeartbeat(
            validator_hotkey=self.ss58_hotkey(f"validator:{name}"),
            software_version=software_version,
            protocol_version=protocol_version,
            code_digest=self.hex_digest(f"validator-code:{name}"),
            state=state,
            active_agent_id=active_agent_id,
            first_seen_at=self.days_ago(7),
            system_metrics=system_metrics,
            benchmark_progress=benchmark_progress,
            benchmark_progress_reported=benchmark_progress is not None,
            benchmark_progress_agent_id=(
                active_agent_id if benchmark_progress is not None else None
            ),
            capabilities=capabilities,
            stack=stack,
            reported_at=seen_at,
            seen_at=seen_at,
            signature=self.signature(f"validator-hb:{name}"),
        )
        session.add(row)
        await session.flush()
        return row

    async def screener_heartbeat(
        self,
        session: AsyncSession,
        *,
        name: str,
        instance_id: str = "sim-1",
        state: str = "polling",
        software_version: str = "0.9.0",
        protocol_version: int = 4,
        seen_ago_seconds: float = 30.0,
        active_agent_id: UUID | None = None,
        screening_progress: dict[str, Any] | None = None,
        system_metrics: dict[str, Any] | None = None,
    ) -> ScreenerHeartbeat:
        """Insert one screener heartbeat row (v2 metrics envelope).

        Progress lives inside ``system_metrics`` as the
        ``{"system_metrics": ..., "screening_progress": ...}`` envelope, the
        shape the public screeners endpoint unwraps.
        """
        seen_at = self.now - timedelta(seconds=seen_ago_seconds)
        if system_metrics is None:
            system_metrics = self.system_metrics(f"screener:{name}:{instance_id}")
        # Always carry the ``screening_progress`` key (even when None): the
        # public reader only treats the stored blob as the v2 envelope when the
        # key is present; otherwise it validates the whole envelope as legacy
        # flat metrics, which fails and renders health as "unknown".
        envelope: dict[str, Any] = {
            "system_metrics": system_metrics,
            "screening_progress": screening_progress,
        }
        row = ScreenerHeartbeat(
            screener_hotkey=self.ss58_hotkey(f"screener:{name}"),
            instance_id=instance_id,
            software_version=software_version,
            protocol_version=protocol_version,
            policy_version=SCREENING_POLICY_VERSION,
            state=state,
            active_agent_id=active_agent_id,
            first_seen_at=self.days_ago(7),
            system_metrics=envelope,
            reported_at=seen_at,
            seen_at=seen_at,
            signature=self.signature(f"screener-hb:{name}:{instance_id}"),
        )
        session.add(row)
        await session.flush()
        return row

    # ── agent object-graph builders ──────────────────────────────────────

    async def uploaded_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> Agent:
        """Agent waiting for screening (``uploaded``) + its payment row."""
        agent = self._new_agent(
            index=index,
            status=AgentStatus.UPLOADED,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at or self.minutes_ago(20 + index),
        )
        session.add(agent)
        await session.flush()
        session.add(self._payment(agent))
        await session.flush()
        return agent

    async def screening_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        screener_name: str = "screener-1",
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[Agent, ScreeningAttempt]:
        """Agent mid-screening: status ``screening`` + one ``running`` attempt.

        Pair with :meth:`screener_heartbeat` (same ``screener_name``, state
        ``screening``, ``active_agent_id=agent.agent_id``) to light up the
        public screeners panel.
        """
        agent = self._new_agent(
            index=index,
            status=AgentStatus.SCREENING,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at or self.minutes_ago(30 + index),
        )
        started = self.minutes_ago(8)
        attempt = ScreeningAttempt(
            attempt_id=self.uuid(f"attempt:{agent.agent_id}"),
            agent_id=agent.agent_id,
            screener_hotkey=self.ss58_hotkey(f"screener:{screener_name}"),
            policy_version=SCREENING_POLICY_VERSION,
            status="running",
            started_at=started,
            deadline=started + timedelta(hours=1),
        )
        session.add(agent)
        await session.flush()
        session.add_all([self._payment(agent), attempt])
        await session.flush()
        return agent, attempt

    async def evaluating_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        issued_to: Sequence[str] = (),
        scored_by: Sequence[str] = (),
        composite: float = 0.55,
        bench_version: int = DEFAULT_BENCH_VERSION,
        n_cases: int = 120,
        run_size: str = "medium",
        median_ms: int | None = None,
        rich_details: bool = False,
        screener_name: str = "screener-1",
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> Agent:
        """Agent in validation: passed screening, dataset pinned, tickets open.

        ``issued_to`` / ``scored_by`` are validator *names* (as passed to
        :meth:`validator_heartbeat`); each name in ``scored_by`` gets a
        ``scored`` ticket + a score row + an audit entry, each name in
        ``issued_to`` gets a live ``issued`` ticket. NOTE: the DB allows at
        most ONE issued ticket per validator hotkey across all agents — the
        scenario is responsible for not reusing an ``issued_to`` name.
        """
        agent = self._new_agent(
            index=index,
            status=AgentStatus.EVALUATING,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at or self.hours_ago(3 + index),
        )
        dataset = self._pin_dataset(
            agent, bench_version=bench_version, run_size=run_size
        )
        session.add(agent)
        await session.flush()
        session.add_all([self._payment(agent), dataset])
        await session.flush()
        await self._passed_attempt(
            session,
            agent,
            screener_hotkey=self.ss58_hotkey(f"screener:{screener_name}"),
            with_image=True,
        )
        issued_at = agent.created_at + timedelta(minutes=45)
        for i, validator_name in enumerate(issued_to):
            session.add(
                ValidatorTicket(
                    agent_id=agent.agent_id,
                    validator_hotkey=self.ss58_hotkey(f"validator:{validator_name}"),
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=self.minutes_ago(10 + i),
                    deadline=self.minutes_from_now(50 - i),
                    bench_version=bench_version,
                )
            )
        for i, validator_name in enumerate(scored_by):
            hotkey = self.ss58_hotkey(f"validator:{validator_name}")
            generated_at = issued_at + timedelta(minutes=30 + 15 * i)
            deadline = issued_at + timedelta(hours=2)
            score = self._score_row(
                agent,
                validator_hotkey=hotkey,
                composite=composite + 0.01 * (i - len(scored_by) // 2),
                bench_version=bench_version,
                n_cases=n_cases,
                generated_at=generated_at,
                ticket_deadline=deadline,
                median_ms=median_ms,
                rich_details=rich_details,
            )
            session.add_all(
                [
                    score,
                    ValidatorTicket(
                        agent_id=agent.agent_id,
                        validator_hotkey=hotkey,
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=issued_at,
                        deadline=deadline,
                        bench_version=bench_version,
                    ),
                ]
            )
            await append_audit_entry(
                session,
                agent_id=agent.agent_id,
                validator_hotkey=hotkey,
                event=EVENT_SCORE,
                payload=self._score_audit_payload(score),
                recorded_at=generated_at,
            )
        await session.flush()
        return agent

    async def finalized_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        composite: float = 0.72,
        composites: Sequence[float] | None = None,
        median_ms: int | None = None,
        rich_details: bool = False,
        validator_names: Sequence[str] = ("validator-1", "validator-2", "validator-3"),
        bench_version: int = DEFAULT_BENCH_VERSION,
        n_cases: int = 120,
        run_size: str = "medium",
        status: AgentStatus = AgentStatus.SCORED,
        screener_name: str = "screener-1",
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
        version: int = 1,
    ) -> Agent:
        """A fully finalized (``scored``/``live``) agent with its whole graph.

        Inserts: agent (dataset pinned + verified screened-image fields),
        benchmark_datasets row, evaluation_payments row, passed screening
        attempt + verified image upload, three ``scored`` tickets, three score
        rows (seed == dataset pin, composites jittered around ``composite``),
        and hash-chained audit entries (3x ``score`` + 1x ``agent_finalized``).

        With defaults (``n_cases=120 >= 100``, composite > 0) the agent is
        leaderboard-eligible; medians land near ``composite``. Pass
        ``composites`` (exactly 3 values) to pin each validator's composite
        exactly (no jitter), and ``median_ms`` to pin every score's latency.
        """
        if len(validator_names) != 3:
            raise ValueError("finalized_agent requires exactly 3 validator names")
        if composites is not None and len(composites) != 3:
            raise ValueError("finalized_agent composites requires exactly 3 values")
        agent = self._new_agent(
            index=index,
            status=status,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at or self.days_ago(2) - timedelta(hours=index),
            version=version,
        )
        dataset = self._pin_dataset(
            agent, bench_version=bench_version, run_size=run_size
        )
        session.add(agent)
        await session.flush()
        session.add_all([self._payment(agent), dataset])
        await session.flush()
        await self._passed_attempt(
            session,
            agent,
            screener_hotkey=self.ss58_hotkey(f"screener:{screener_name}"),
            with_image=True,
        )

        issued_at = agent.created_at + timedelta(minutes=45)
        deadline = issued_at + timedelta(hours=2)
        offsets = (-0.012, 0.0, 0.015)
        scores: list[Score] = []
        for i, validator_name in enumerate(validator_names):
            hotkey = self.ss58_hotkey(f"validator:{validator_name}")
            generated_at = issued_at + timedelta(minutes=25 + 20 * i)
            score = self._score_row(
                agent,
                validator_hotkey=hotkey,
                composite=(
                    composites[i] if composites is not None else composite + offsets[i]
                ),
                bench_version=bench_version,
                n_cases=n_cases,
                generated_at=generated_at,
                ticket_deadline=deadline,
                median_ms=median_ms,
                rich_details=rich_details,
            )
            scores.append(score)
            session.add_all(
                [
                    score,
                    ValidatorTicket(
                        agent_id=agent.agent_id,
                        validator_hotkey=hotkey,
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=issued_at,
                        deadline=deadline,
                        bench_version=bench_version,
                    ),
                ]
            )
            await append_audit_entry(
                session,
                agent_id=agent.agent_id,
                validator_hotkey=hotkey,
                event=EVENT_SCORE,
                payload=self._score_audit_payload(score),
                recorded_at=generated_at,
            )
        median_composite = sorted(s.composite for s in scores)[1]
        await append_audit_entry(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=None,
            event=EVENT_FINALIZED,
            payload={
                "median_composite": median_composite,
                "quorum": len(scores),
                "bench_version": bench_version,
                "validators": [s.validator_hotkey for s in scores],
                "dataset": {
                    "seed": dataset.seed,
                    "sha256": dataset.sha256,
                    "run_size": dataset.run_size,
                },
            },
            recorded_at=scores[-1].generated_at + timedelta(seconds=5),
        )
        await session.flush()
        return agent

    async def quarantined_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        reason_code: str = "policy-dynamic-exec",
        screener_name: str = "screener-1",
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[Agent, ScreeningQuarantine]:
        """Agent held ``quarantined``: attempt ``quarantined`` + active quarantine."""
        agent = self._new_agent(
            index=index,
            status=AgentStatus.QUARANTINED,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at or self.hours_ago(6 + index),
        )
        started = agent.created_at + timedelta(minutes=5)
        finished = started + timedelta(minutes=18)
        attempt = ScreeningAttempt(
            attempt_id=self.uuid(f"attempt:{agent.agent_id}"),
            agent_id=agent.agent_id,
            screener_hotkey=self.ss58_hotkey(f"screener:{screener_name}"),
            policy_version=SCREENING_POLICY_VERSION,
            status="quarantined",
            started_at=started,
            deadline=started + timedelta(hours=1),
            finished_at=finished,
            reason_code=reason_code,
        )
        quarantine = ScreeningQuarantine(
            quarantine_id=self.uuid(f"quarantine:{agent.agent_id}"),
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            screener_hotkey=attempt.screener_hotkey,
            policy_version=SCREENING_POLICY_VERSION,
            manifest_digest=self.hex_digest(f"manifest:{agent.agent_id}"),
            reason_code=reason_code,
            evidence=[
                {
                    "module": "src/agent.rs",
                    "code": reason_code,
                    "summary": "simulated policy finding",
                    "digest": self.hex_digest(f"evidence:{agent.agent_id}"),
                }
            ],
            status="active",
            created_at=finished,
        )
        session.add(agent)
        await session.flush()
        session.add_all([self._payment(agent), attempt])
        await session.flush()
        session.add(quarantine)
        await session.flush()
        return agent, quarantine

    async def rejected_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        screening_reason: str = "build failed",
        screening_reason_code: str = "build-compile-error",
        screener_name: str = "screener-1",
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> Agent:
        """Agent deterministically ``rejected`` at screening, with public reason."""
        agent = self._new_agent(
            index=index,
            status=AgentStatus.REJECTED,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at or self.hours_ago(10 + index),
        )
        agent.screening_reason = screening_reason
        agent.screening_reason_code = screening_reason_code
        started = agent.created_at + timedelta(minutes=5)
        attempt = ScreeningAttempt(
            attempt_id=self.uuid(f"attempt:{agent.agent_id}"),
            agent_id=agent.agent_id,
            screener_hotkey=self.ss58_hotkey(f"screener:{screener_name}"),
            policy_version=SCREENING_POLICY_VERSION,
            status="rejected",
            started_at=started,
            deadline=started + timedelta(hours=1),
            finished_at=started + timedelta(minutes=12),
            public_reason=screening_reason,
            reason_code=screening_reason_code,
        )
        session.add(agent)
        await session.flush()
        session.add_all([self._payment(agent), attempt])
        await session.flush()
        return agent

    async def ath_review_agent(
        self,
        session: AsyncSession,
        *,
        index: int,
        original: Agent,
        composite: float = 0.8,
        validator_names: Sequence[str] = ("validator-1", "validator-2", "validator-3"),
        bench_version: int = DEFAULT_BENCH_VERSION,
        miner_hotkey: str | None = None,
        name: str | None = None,
        created_at: datetime | None = None,
    ) -> tuple[Agent, AthReview]:
        """Agent held in ``ath_pending_review`` as a suspected copy of ``original``.

        Builds the full finalized graph (screening pass, tickets, scores,
        audit) but leaves status at ``ath_pending_review`` with a pending
        :class:`AthReview` and ``duplicate_of`` pointing at ``original``.
        """
        agent = await self.finalized_agent(
            session,
            index=index,
            composite=composite,
            validator_names=validator_names,
            bench_version=bench_version,
            status=AgentStatus.ATH_PENDING_REVIEW,
            miner_hotkey=miner_hotkey,
            name=name,
            created_at=created_at,
        )
        agent.duplicate_of = original.agent_id
        agent.review_reason = (
            f"anti-copy hold: near-duplicate of {original.name} v{original.version}"
        )
        review = AthReview(
            review_id=self.uuid(f"ath-review:{agent.agent_id}"),
            agent_id=agent.agent_id,
            status="pending",
            opened_at=self.hours_ago(1),
            original_duplicate_of=original.agent_id,
            original_reason=agent.review_reason,
            original_policy_version=SCREENING_POLICY_VERSION,
            original_evidence={
                "content_similarity": 0.97,
                "size_delta_bytes": 412,
                "channel": "content_fingerprint",
            },
            algorithm_provenance={
                "algorithm": "minhash-jaccard",
                "k": 256,
                "threshold": 0.9,
            },
        )
        session.add(review)
        await session.flush()
        return agent, review

    # ── payload shaping ──────────────────────────────────────────────────

    @staticmethod
    def _score_audit_payload(score: Score) -> dict[str, Any]:
        """Public-safe audit payload mirroring what the score path records."""
        return {
            "run_id": score.run_id,
            "seed": score.seed,
            "bench_version": score.bench_version,
            "composite": score.composite,
            "tool_mean": score.tool_mean,
            "memory_mean": score.memory_mean,
            "median_ms": score.median_ms,
            "n": score.n,
            "signature": score.signature,
            "generated_at": score.generated_at.astimezone(UTC).isoformat(),
        }
