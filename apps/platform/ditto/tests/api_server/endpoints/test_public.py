"""Unit tests for :mod:`ditto.api_server.endpoints.public`.

``GET /api/v1/public/leaderboard`` is open (no validator auth) and aggregate-only:
it must rank miners by composite, expose tool/memory means, and NEVER leak the
integrity-internal fields (``signature``, ``sha256``, ``validator_hotkey``) or
per-case detail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models import bench_glossary as bench_glossary_data
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.public import PublicBenchmarkProgress, PublicSystemMetrics
from ditto.api_models.screener import (
    SCREENING_POLICY_VERSION,
    SourceReviewEvidenceItem,
    SourceReviewFinding,
)
from ditto.api_models.stack_health import (
    ComponentHealthState,
    ValidatorComponentHealth,
    ValidatorStackHealth,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.bench import CURRENT_BENCH_VERSION
from ditto.api_server.crn import champion_anchored_seeds
from ditto.api_server.datapipeline import DataPipelineError
from ditto.api_server.dependencies import (
    get_dataset_generator,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints import public as public_endpoint
from ditto.api_server.endpoints.public import _fleet_classification
from ditto.api_server.koth import TOP5_MAX_CONFIRMATION_SEEDS
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.api_server.validator_names import ValidatorNamesSnapshot
from ditto.api_server.validator_slot_settings import (
    DEFAULT_SETTINGS as SLOT_SETTINGS_DEFAULT,
)
from ditto.chain import ChainError
from ditto.chain.models import (
    ChainWeight,
    ChainWeightsSnapshot,
    ChainWeightVector,
)
from ditto.db.models import (
    Agent,
    AgentKingship,
    ArtifactFetchAudit,
    ArtifactReleaseSettingsRevision,
    AthReview,
    AthReviewAction,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    EvaluationPayment,
    InferenceGrant,
    OwnerAttestation,
    Score,
    ScreeningAttempt,
    ScreeningQuarantine,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE,
    GENESIS_HASH,
    append_audit_entry,
)
from ditto.db.queries.benchmark_rollout import (
    DEFAULT_BENCH_VERSION,
    LEGACY_BENCH_VERSION,
    MIN_SCOREABLE_BENCH_VERSION,
)
from ditto.db.queries.scores import upsert_score
from ditto.tests.legacy_era import (
    grandfather_active_era,
    retired_era_writes_allowed,
)

_MINER_A = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_MINER_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_VALIDATOR_C = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
# The era every generic fixture in this file sits on. Almost nothing here is
# about a particular benchmark generation -- these tests need *a* score, and v2
# was only ever the value ``DEFAULT_BENCH_VERSION`` happened to hold. The
# bench-version floor refuses to write anything below
# ``MIN_SCOREABLE_BENCH_VERSION``, so a fixture that wants a score the ledger
# can still serve has to write one on an era that can still be scored.
_ERA = MIN_SCOREABLE_BENCH_VERSION
# A second, distinct era for the handful of tests that separate one generation
# from another. Raw ``scores``/``confirmation_scores`` rows are only floored, so
# a version above the newest shipped contract is fine there -- but never feed
# this to ticket issuance or a rollout target, because ``benchmark_contract(8)``
# does not exist.
_NEXT_ERA = _ERA + 1
# The newest *retired* era: the last generation below the floor. Rows on it can
# only be written through ``retired_era_writes_allowed``, which is the point --
# production still holds and still publishes its pre-floor history, so a test
# about "the previous generation" is a test about a version the ledger would
# now refuse. Never a generic fixture value; use ``_ERA`` for that.
_PREV_ERA = _ERA - 1


async def _activate_era(
    maker: async_sessionmaker[AsyncSession], version: int = _ERA
) -> None:
    """Record the activated rollout that makes ``version`` the ledger authority.

    ``list_eligible_ledger`` serves exactly one version -- the active one -- so
    a board built from scores on any other era comes back empty. Production
    reaches an era through an activated rollout, and a fixture that scores on
    one has to say so as well rather than leaning on whatever the no-activation
    fallback happens to answer.

    Keep this explicit even for ``_ERA``, where the fallback currently agrees by
    coincidence: the floor is a floor, and it will move again.
    """
    async with maker() as s, s.begin():
        s.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=DEFAULT_BENCH_VERSION,
                desired_version=version,
                status="activated",
                cohort_size=5,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                activated_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )


def _screened_image(agent_id: UUID, verified_at: datetime) -> dict:
    """The verified screened-image columns the live contract demands.

    From v7 on, ``queue_candidate_predicate`` filters out every submission that
    has no fully verified screened image, so a fixture that omits these columns
    is not merely missing metadata -- its agent silently leaves the queue and
    the rank being asserted is a rank nobody is standing in.
    """
    return {
        "screened_image_sha256": "12" * 32,
        "screened_image_size_bytes": 123,
        "screened_image_id": "sha256:" + "34" * 32,
        "screened_image_ref": f"ditto-screen/{agent_id}:latest",
        "screened_image_upload_id": uuid4(),
        "screened_image_verified_at": verified_at,
    }


def _dataset_pin(
    agent_id: UUID,
    bench_version: int = _ERA,
    *,
    seed: int = 1,
    sha256: str = "cd" * 32,
    run_size: str = "full",
) -> BenchmarkDataset:
    """The per-agent dataset pin every post-v2 contract requires to lease."""
    return BenchmarkDataset(
        agent_id=agent_id,
        bench_version=bench_version,
        seed=seed,
        sha256=sha256,
        run_size=run_size,
    )


async def _take_authority(
    maker: async_sessionmaker[AsyncSession],
    *,
    rollout_id: UUID,
    at: datetime,
) -> None:
    """Hand ledger authority to a rollout that is still collecting.

    ``persisted_active_bench_version`` honours an ``authority_selected`` audit
    event as well as an activation, which is how a transition can be the era in
    force while its qualification is still settling -- the state these fixtures
    describe. They used to reach it for free, because the era in force was
    ``DEFAULT_BENCH_VERSION`` and the rollout happened to target it; now that
    the target sits above the default, the transfer has to be written down.
    """
    async with maker() as s, s.begin():
        s.add(
            BenchmarkRolloutAudit(
                audit_id=uuid4(),
                rollout_id=rollout_id,
                event="authority_selected",
                payload={},
                recorded_at=at,
            )
        )


def _anchor_seeds(champion_id: str, count: int = 3) -> tuple[int, ...]:
    """The first ``count`` CRN seeds the given champion's reign anchors on.

    Fixtures cannot invent seed values any more. The fold is scoped to the
    reigning champion's anchor, so rows on seeds no champion would ever issue
    are precisely the stale-reign evidence that scoping exists to exclude --
    a fixture using them would assert against a wave the lane cannot produce.
    """
    return tuple(
        champion_anchored_seeds(
            UUID(champion_id),
            version=_ERA,
            max_seeds=TOP5_MAX_CONFIRMATION_SEEDS,
        )[:count]
    )


def _scorer_capabilities(now: datetime, *, versions: list[int]) -> dict:
    return {
        "screened_images": True,
        "require_screened_image": True,
        "source_build_fallback": False,
        "full_stack_managed": True,
        "stack_updater": True,
        "sandbox_egress_restricted": True,
        "ticket_inference": False,
        "signed_score_quorum": False,
        "executor_isolation": "ephemeral_vm",
        "scorer_benchmarks": {
            "status": "fresh_verified",
            "supported_bench_versions": versions,
            "observed_at": int(now.timestamp()),
            "software_version": "1.0.0",
            "source_revision": "a" * 40,
        },
    }


def test_v5_token_telemetry_public_parser_is_typed_and_fail_closed() -> None:
    details = {
        "token_usage": {
            "accounting_version": 2,
            "status": "complete",
            "source": "model_proxy_provider_response",
            "provider": "openrouter",
            "profile_revision": "profile-v1",
            "model": "qwen/qwen3-32b",
            "prompt_tokens": 1800,
            "prompt_bytes": 7200,
            "completion_tokens": 200,
            "total_tokens": 2000,
            "requests": 10,
            "successes": 10,
            "usage_available": 10,
            "usage_unavailable": 0,
            "provider_latency_ms": 2500,
            "ttft_status": "unavailable_non_streaming",
        },
        "token_efficiency": {
            "formula_version": "v5-relay-token-waste-p90-v1",
            "baseline_id": "v5-baseline",
            "baseline_prompt_tokens": 900,
            "baseline_completion_tokens": 100,
            "baseline_total_tokens": 1000,
            "budget_percentile": 0.9,
            "observed_prompt_tokens": 1800,
            "observed_completion_tokens": 200,
            "observed_total_tokens": 2000,
            "excess_ratio": 1.0,
            "maximum_penalty": 0.1,
            "minimum_multiplier": 0.9,
            "multiplier": 0.95,
            "raw_composite": 0.9,
            "adjusted_composite": 0.855,
            "penalty_applied": True,
            "decision_reason": "above_budget",
        },
    }
    usage = public_endpoint._safe_token_usage(details)
    decision = public_endpoint._safe_token_efficiency(details)
    assert usage is not None and usage.total_tokens == 2000
    assert decision is not None and decision.adjusted_composite == 0.855
    assert decision.penalty_applied is True

    details["token_efficiency"]["multiplier"] = 1.001
    assert public_endpoint._safe_token_efficiency(details) is None


def test_composite_breakdown_separates_quality_gates_from_token_penalty() -> None:
    details = {
        "raw_composite": 0.372854,
        "token_efficiency": {
            "formula_version": "v5-relay-token-waste-p90-v1",
            "baseline_id": "v5-baseline",
            "baseline_prompt_tokens": 1_200_000,
            "baseline_completion_tokens": 291_793,
            "baseline_total_tokens": 1_491_793,
            "budget_percentile": 0.9,
            "observed_prompt_tokens": 1_500_000,
            "observed_completion_tokens": 364_699,
            "observed_total_tokens": 1_864_699,
            "excess_ratio": 0.25,
            "maximum_penalty": 0.1,
            "minimum_multiplier": 0.9,
            "multiplier": 0.9800018,
            "raw_composite": 0.372854,
            "adjusted_composite": 0.365398,
            "penalty_applied": True,
            "decision_reason": "above_budget",
        },
    }

    breakdown = public_endpoint._composite_breakdown(
        tool_mean=0.9278788,
        memory_mean=0.5729167,
        final_composite=0.365398,
        details=details,
    )

    assert breakdown is not None
    assert breakdown.base_accuracy == pytest.approx(0.75039775)
    assert breakdown.benchmark_quality_multiplier == pytest.approx(
        0.372854 / 0.75039775
    )
    assert breakdown.pre_token_composite == 0.372854
    assert breakdown.token_efficiency_multiplier == pytest.approx(0.9800018)
    assert breakdown.token_penalty == pytest.approx(0.0199982)
    assert breakdown.maximum_token_penalty == 0.1
    assert breakdown.final_composite == 0.365398


def test_composite_breakdown_exposes_public_safe_quality_factor_telemetry() -> None:
    breakdown = public_endpoint._composite_breakdown(
        tool_mean=0.9,
        memory_mean=0.7,
        final_composite=0.64,
        details={
            "raw_composite": 0.64,
            "tool_efficiency": 0.8,
            "metamorphic_consistency": 0.75,
            "conversational_sanity": 0.9,
            "transform_robustness": 0.8,
            "audit_case_count": 12,
            "expected": "must never leak",
        },
    )

    assert breakdown is not None
    factors = {factor.key: factor for factor in breakdown.quality_factors}
    assert factors["tool_efficiency"].multiplier == pytest.approx(0.8)
    assert factors["metamorphic_consistency"].metric == pytest.approx(0.75)
    assert factors["conversational_sanity"].metric == pytest.approx(0.9)
    assert factors["transform_robustness"].audit_count == 12
    assert "other_quality_effects" not in factors
    assert "expected" not in breakdown.model_dump_json()


def test_composite_breakdown_shows_no_token_penalty_when_within_budget() -> None:
    details = {
        "raw_composite": 0.493952,
        "token_efficiency": {
            "formula_version": "v5-relay-token-waste-p90-v1",
            "baseline_id": "v5-baseline",
            "baseline_prompt_tokens": 1_200_000,
            "baseline_completion_tokens": 291_793,
            "baseline_total_tokens": 1_491_793,
            "budget_percentile": 0.9,
            "observed_prompt_tokens": 1_000_000,
            "observed_completion_tokens": 283_639,
            "observed_total_tokens": 1_283_639,
            "excess_ratio": 0.0,
            "maximum_penalty": 0.1,
            "minimum_multiplier": 0.9,
            "multiplier": 1.0,
            "raw_composite": 0.493952,
            "adjusted_composite": 0.493952,
            "penalty_applied": False,
            "decision_reason": "within_budget",
        },
    }

    breakdown = public_endpoint._composite_breakdown(
        tool_mean=0.8018181818,
        memory_mean=0.8333333333,
        final_composite=0.493952,
        details=details,
    )

    assert breakdown is not None
    assert breakdown.base_accuracy == pytest.approx(0.81757575755)
    assert breakdown.benchmark_quality_multiplier == pytest.approx(
        0.493952 / 0.81757575755
    )
    assert breakdown.token_efficiency_multiplier == 1.0
    assert breakdown.token_penalty == 0.0


# The generator release each bench version pins, and the version flag that
# release accepts. These are not cosmetic: v0.7.0 predates `-bench-version` and
# fails with "flag provided but not defined" if it is passed, while every
# release from v0.8.0 on requires it (the flag defaults to 0 and the binary
# exits 2 with "-bench-version is required"). Each row below was verified by
# running the rendered command against the real generator and confirming it
# exits 0 and prints a dataset_sha256.
_EXPECTED_DATASET_COMMANDS = (
    (2, "v0.7.0", ""),
    (3, "v0.8.0", " -bench-version 3"),
    (4, "v0.9.0", " -bench-version 4"),
    (5, "v0.10.0", " -bench-version 5"),
    (6, "v0.11.1", " -bench-version 6"),
    (7, "v0.12.0", " -bench-version 7"),
)


def test_datagen_version_map_pins_every_supported_bench_version() -> None:
    """The pins are an immutable public contract; 2-6 must never drift."""
    assert public_endpoint._DATAGEN_VERSION_BY_BENCH_VERSION == {
        2: "v0.7.0",
        3: "v0.8.0",
        4: "v0.9.0",
        5: "v0.10.0",
        6: "v0.11.1",
        7: "v0.12.0",
    }


@pytest.mark.parametrize(
    ("bench_version", "datagen_version", "version_flag"), _EXPECTED_DATASET_COMMANDS
)
def test_dataset_command_renders_a_runnable_generator_invocation(
    bench_version: int, datagen_version: str, version_flag: str
) -> None:
    """Both public commands must actually run, not just look plausible.

    A command that exits 2 is worse than no command at all: it still reads as
    auditable to anyone skimming the leaderboard.
    """
    base = (
        "go run github.com/ditto-assistant/dittobench-datagen/cmd/"
        f"generate@{datagen_version}{version_flag} -seed 987654321 -run-size full"
    )

    verification = public_endpoint._dataset_command(
        seed=987654321,
        run_size="full",
        bench_version=bench_version,
        sha_only=True,
    )
    reproduction = public_endpoint._dataset_command(
        seed=987654321,
        run_size="full",
        bench_version=bench_version,
        sha_only=False,
    )

    assert verification == f"{base} -sha"
    assert reproduction == f"{base} -out dataset.json"


def test_dataset_command_omits_version_flag_only_for_the_release_lacking_it() -> None:
    """v0.7.0 rejects `-bench-version`; v0.8.0+ require it."""
    for bench_version, _, _ in _EXPECTED_DATASET_COMMANDS:
        command = public_endpoint._dataset_command(
            seed=1,
            run_size="small",
            bench_version=bench_version,
            sha_only=True,
        )
        assert command is not None
        assert ("-bench-version" in command) is (bench_version != 2)


def test_dataset_command_is_withheld_when_the_generator_pin_is_unknown() -> None:
    """An unpinned epoch renders nothing rather than an unrunnable command."""
    assert (
        public_endpoint._dataset_command(
            seed=1, run_size="full", bench_version=8, sha_only=True
        )
        is None
    )
    assert (
        public_endpoint._dataset_command(
            seed=1, run_size="full", bench_version=None, sha_only=True
        )
        is None
    )
    assert (
        public_endpoint._dataset_command(
            seed=1, run_size="enormous", bench_version=7, sha_only=True
        )
        is None
    )


def test_future_dataset_command_uses_exact_monorepo_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        public_endpoint._DATAGEN_MONOREPO_REF_BY_BENCH_VERSION,
        8,
        "v1.2.3",
    )
    command = public_endpoint._dataset_command(
        seed=17, run_size="full", bench_version=8, sha_only=False
    )

    assert command is not None
    assert "https://github.com/ditto-assistant/ditto-subnet.git" in command
    assert "fetch --depth=1 origin v1.2.3" in command
    assert 'cd "$tmp/ditto-subnet/research/dittobench-datagen"' in command
    assert "go run ./cmd/generate -bench-version 8" in command
    assert '-out "$output"' in command


def _install_db(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session


async def _seed_scored(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner: str,
    composite: float,
    tool_mean: float,
    memory_mean: float,
    status: AgentStatus = AgentStatus.SCORED,
    median_ms: int = 500,
    generated_at: datetime = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
    recorded_at: datetime | None = None,
    details: dict | None = None,
    bench_version: int = _ERA,
) -> None:
    async with maker() as s, s.begin():
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey=miner,
            name="agent",
            sha256="ab" * 32,
            size_bytes=524288,
            status=status,
            created_at=datetime.now(UTC),
        )
        s.add(agent)
        await s.flush()
        await upsert_score(
            s,
            agent_id=agent.agent_id,
            validator_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            run_id="run_1",
            seed=42,
            composite=composite,
            tool_mean=tool_mean,
            memory_mean=memory_mean,
            median_ms=median_ms,
            n=20,
            generated_at=generated_at,
            signature="ab" * 64,
            details=details,
            bench_version=bench_version,
        )
        if recorded_at is not None:
            score = await s.get(
                Score,
                (
                    agent.agent_id,
                    bench_version,
                    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                ),
            )
            assert score is not None
            score.created_at = recorded_at
            score.updated_at = recorded_at


async def _seed_k3(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner: str,
    composites: list[float],
    status: AgentStatus = AgentStatus.SCORED,
    dataset_seed: int | None = 987654321,
    dataset_sha256: str | None = "cd" * 32,
    dataset_run_size: str | None = "full",
    dataset_seed_block: int | None = 4321,
    dataset_seed_block_hash: str | None = "0x" + "9f" * 32,
    details: dict | None = None,
    base_time: datetime = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
    created_at: datetime | None = None,
    accepted_tickets: bool = True,
    queue_ready: bool = True,
) -> str:
    """Seed one agent scored by ``len(composites)`` distinct validators.

    Returns the agent_id (hex str) so a test can hit the detail endpoint.

    ``accepted_tickets`` records the SCORED ticket each score came from, which
    is the only shape production ever has: a validator cannot post a score
    without holding a ticket for the submission. It matters because the
    allocator's contender lane counts *accepted tickets*, not recorded scores,
    so a fixture with scores and no tickets silently disables the lane it is
    trying to exercise.

    ``queue_ready`` writes the verified screened image and the dataset pin the
    live contract requires of a queue candidate. Same reasoning: from v7 on, a
    submission missing either is filtered out of the queue entirely, so a
    fixture without them quietly stops exercising the lane it set up. Pass
    ``False`` only when the submission is meant to be unservable.
    """
    validators = [
        "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
        "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp",
    ]
    agent_id = uuid4()
    bench_version = int(details.get("bench_version", _ERA)) if details else _ERA
    async with maker() as s, s.begin():
        agent = Agent(
            agent_id=agent_id,
            miner_hotkey=miner,
            name="agent",
            sha256="ab" * 32,
            size_bytes=524288,
            status=status,
            dataset_seed=dataset_seed,
            dataset_sha256=dataset_sha256,
            dataset_run_size=dataset_run_size,
            dataset_seed_block=dataset_seed_block,
            dataset_seed_block_hash=dataset_seed_block_hash,
            created_at=created_at or datetime.now(UTC),
            **(
                _screened_image(agent_id, created_at or datetime.now(UTC))
                if queue_ready
                else {}
            ),
        )
        s.add(agent)
        await s.flush()
        # The pin mirrors the agent's own dataset columns: a fixture that says
        # this submission has no dataset must not be handed one through the
        # back door, because "no pinned dataset" is a state the pipeline
        # reports on.
        if queue_ready and dataset_seed is not None and dataset_sha256 is not None:
            s.add(
                _dataset_pin(
                    agent_id,
                    bench_version,
                    seed=dataset_seed,
                    sha256=dataset_sha256,
                    run_size=dataset_run_size or "full",
                )
            )
        for i, composite in enumerate(composites):
            await upsert_score(
                s,
                agent_id=agent_id,
                validator_hotkey=validators[i],
                run_id=f"run_{i}",
                seed=dataset_seed or 0,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=500,
                n=110,
                generated_at=base_time + timedelta(minutes=i),
                signature="ab" * 64,
                details=details,
                bench_version=bench_version,
            )
            if accepted_tickets:
                s.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        bench_version=bench_version,
                        validator_hotkey=validators[i],
                        slot_id="slot-0",
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=base_time + timedelta(minutes=i),
                        deadline=base_time + timedelta(minutes=i + 90),
                        attempt_count=1,
                        manual_retry_grants=0,
                    )
                )
    return str(agent_id)


async def _seed_top_five_floor(
    maker: async_sessionmaker[AsyncSession],
    *,
    fifth_place: float = 0.80,
    bench_version: int = _ERA,
) -> None:
    for rank, marker in enumerate(("A", "B", "C", "D", "E")):
        composite = fifth_place + (4 - rank) * 0.01
        await _seed_k3(
            maker,
            miner="5" + marker * 47,
            composites=[composite, composite, composite],
            details={"bench_version": bench_version},
        )


async def _seed_top_ten_floor(
    maker: async_sessionmaker[AsyncSession],
    *,
    tenth_place: float = 0.60,
    bench_version: int = _ERA,
) -> None:
    for rank, marker in enumerate("ABCDEFGHJK"):
        composite = tenth_place + (9 - rank) * 0.01
        await _seed_k3(
            maker,
            miner="5" + marker * 47,
            composites=[composite, composite, composite],
            details={"bench_version": bench_version},
        )


async def _seed_agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner: str,
    status: AgentStatus = AgentStatus.UPLOADED,
    name: str = "agent",
    created_at: datetime | None = None,
    screening_reason: str | None = None,
    duplicate_of: UUID | None = None,
    review_reason: str | None = None,
    screening_policy_version: int = 0,
    queue_ready: bool = True,
) -> str:
    """Seed a submission with no score (e.g. still uploaded/evaluating)."""
    agent_id = uuid4()
    arrived_at = created_at or datetime.now(UTC)
    async with maker() as s, s.begin():
        s.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=miner,
                name=name,
                sha256="cd" * 32,
                size_bytes=524288,
                status=status,
                created_at=arrived_at,
                screening_reason=screening_reason,
                duplicate_of=duplicate_of,
                review_reason=review_reason,
                screening_policy_version=screening_policy_version,
                **(_screened_image(agent_id, arrived_at) if queue_ready else {}),
            )
        )
        if queue_ready:
            s.add(_dataset_pin(agent_id))
    return str(agent_id)


async def _seed_payment(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: str,
    miner_hotkey: str,
    miner_coldkey: str,
    index: int,
) -> None:
    """Attach a payment-time coldkey to a seeded submission.

    The owner identity the validator ticket allocator and the emission ledger
    both partition on lives here, not on the agent row.
    """
    async with maker() as s, s.begin():
        s.add(
            EvaluationPayment(
                block_hash=f"0xpayment-{index}",
                extrinsic_index=index,
                agent_id=UUID(agent_id),
                miner_hotkey=miner_hotkey,
                miner_coldkey=miner_coldkey,
                amount_rao=1,
                tao_usd_rate=Decimal("1"),
                dest_address="5Destination",
                timestamp=datetime.now(UTC),
            )
        )


async def _drain_weight_refreshes(app: FastAPI) -> None:
    """Await any in-flight background weight-matrix refresh.

    The endpoint refreshes off the request path, so a test that wants to observe
    the *result* of a refresh has to wait for it rather than assume the next
    request sees it.
    """
    tasks = getattr(app.state, "public_chain_weights_tasks", None)
    if isinstance(tasks, set) and tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


def _weights_snapshot() -> ChainWeightsSnapshot:
    """One revealed vector, enough to assert the projection and the cache."""
    return ChainWeightsSnapshot(
        netuid=118,
        block=8_639_503,
        block_hash="0x" + "ab" * 32,
        owner_hotkey=_MINER_B,
        vectors=(
            ChainWeightVector(
                validator_uid=25,
                validator_hotkey=_VALIDATOR_C,
                weights=(ChainWeight(uid=169, hotkey=_MINER_A, value=14745),),
            ),
        ),
    )


class TestPublicChainWeights:
    async def test_returns_native_revealed_weight_matrix(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.state.chain = SimpleNamespace(
            get_weights=AsyncMock(return_value=_weights_snapshot())
        )

        response = await client.get("/api/v1/public/weights")

        assert response.status_code == 200
        assert (
            response.headers["cache-control"]
            == "public, max-age=30, stale-while-revalidate=120"
        )
        body = response.json()
        assert body["netuid"] == 118
        assert body["block"] == 8_639_503
        assert body["block_hash"] == "0x" + "ab" * 32
        assert body["owner_hotkey"] == _MINER_B
        assert body["vectors"] == [
            {
                "validator_uid": 25,
                "validator_hotkey": _VALIDATOR_C,
                "weights": [{"uid": 169, "hotkey": _MINER_A, "value": 14745}],
            }
        ]
        app.state.chain.get_weights.assert_awaited_once_with(118)

    async def test_returns_503_when_chain_read_is_unavailable(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.state.chain = SimpleNamespace(
            get_weights=AsyncMock(side_effect=ChainError("rpc unavailable"))
        )

        response = await client.get("/api/v1/public/weights")

        assert response.status_code == 503
        assert response.json()["message"] == "chain weights unavailable"

    async def test_returns_503_when_chain_client_lacks_weight_read(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.state.chain = SimpleNamespace()

        response = await client.get("/api/v1/public/weights")

        assert response.status_code == 503

    async def test_caches_the_matrix_instead_of_reading_chain_per_request(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.state.chain = SimpleNamespace(
            get_weights=AsyncMock(return_value=_weights_snapshot())
        )

        first = await client.get("/api/v1/public/weights")
        second = await client.get("/api/v1/public/weights")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["block"] == 8_639_503
        assert second.json()["stale"] is False
        # The whole point: N dashboard polls must not become N substrate reads.
        app.state.chain.get_weights.assert_awaited_once_with(118)

    async def test_concurrent_requests_trigger_a_single_chain_read(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        reads = 0

        async def _slow_read(_netuid: int) -> ChainWeightsSnapshot:
            nonlocal reads
            reads += 1
            started.set()
            await release.wait()
            return _weights_snapshot()

        app.state.chain = SimpleNamespace(get_weights=_slow_read)
        first = asyncio.create_task(client.get("/api/v1/public/weights"))
        await started.wait()
        # A second caller arriving mid-read has no cache to fall back on, so it
        # waits on the same single-flight read rather than opening its own.
        second = asyncio.create_task(client.get("/api/v1/public/weights"))
        await asyncio.sleep(0)
        release.set()
        responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        assert reads == 1

    async def test_a_cached_response_never_waits_on_the_chain_read(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = asyncio.Event()

        async def _slow_read(_netuid: int) -> ChainWeightsSnapshot:
            await release.wait()
            return _weights_snapshot()

        app.state.chain = SimpleNamespace(
            get_weights=AsyncMock(return_value=_weights_snapshot())
        )
        assert (await client.get("/api/v1/public/weights")).status_code == 200

        # Cache expired, and the refresh now blocks indefinitely. The request
        # must still return the cached matrix rather than wait out the read.
        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_CACHE_TTL_SECONDS", 0.0)
        app.state.chain = SimpleNamespace(get_weights=_slow_read)

        response = await asyncio.wait_for(
            client.get("/api/v1/public/weights"), timeout=5
        )

        assert response.status_code == 200
        assert response.json()["block"] == 8_639_503
        release.set()
        await _drain_weight_refreshes(app)

    async def test_serves_last_known_good_marked_stale_when_refresh_fails(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        get_weights = AsyncMock(return_value=_weights_snapshot())
        app.state.chain = SimpleNamespace(get_weights=get_weights)
        assert (await client.get("/api/v1/public/weights")).status_code == 200

        # Expire the cache so the next request refreshes, then fail that refresh.
        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_CACHE_TTL_SECONDS", 0.0)
        monkeypatch.setattr(
            public_endpoint, "_CHAIN_WEIGHTS_FAILURE_BACKOFF_SECONDS", 0.0
        )
        get_weights.side_effect = ChainError("rpc unavailable")
        get_weights.return_value = None

        assert (await client.get("/api/v1/public/weights")).status_code == 200
        await _drain_weight_refreshes(app)
        response = await client.get("/api/v1/public/weights")

        # A transient upstream failure must not blank the panel: the last good
        # matrix is still served, explicitly labeled stale.
        assert response.status_code == 200
        body = response.json()
        assert body["stale"] is True
        assert body["block"] == 8_639_503
        assert body["vectors"][0]["validator_hotkey"] == _VALIDATOR_C
        assert body["age_seconds"] >= 0.0

    async def test_serves_last_known_good_when_refresh_times_out(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        get_weights = AsyncMock(return_value=_weights_snapshot())
        app.state.chain = SimpleNamespace(get_weights=get_weights)
        assert (await client.get("/api/v1/public/weights")).status_code == 200

        async def _never_returns(_netuid: int) -> ChainWeightsSnapshot:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_CACHE_TTL_SECONDS", 0.0)
        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(
            public_endpoint, "_CHAIN_WEIGHTS_FAILURE_BACKOFF_SECONDS", 0.0
        )
        app.state.chain = SimpleNamespace(get_weights=_never_returns)

        assert (await client.get("/api/v1/public/weights")).status_code == 200
        await _drain_weight_refreshes(app)
        response = await client.get("/api/v1/public/weights")

        assert response.status_code == 200
        assert response.json()["stale"] is True

    async def test_backs_off_instead_of_retrying_a_failing_read_per_request(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        get_weights = AsyncMock(side_effect=ChainError("rpc unavailable"))
        app.state.chain = SimpleNamespace(get_weights=get_weights)

        first = await client.get("/api/v1/public/weights")
        second = await client.get("/api/v1/public/weights")

        assert [first.status_code, second.status_code] == [503, 503]
        # A 503 is not stored by the response cache, so without this backoff the
        # next poll would immediately run another doomed multi-second read — the
        # loop that made this endpoint fail a quarter of the time in prod.
        assert get_weights.await_count == 1

    async def test_reverts_to_503_once_the_cached_matrix_is_too_old(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        get_weights = AsyncMock(return_value=_weights_snapshot())
        app.state.chain = SimpleNamespace(get_weights=get_weights)
        assert (await client.get("/api/v1/public/weights")).status_code == 200

        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_CACHE_TTL_SECONDS", 0.0)
        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_MAX_STALE_SECONDS", -1.0)
        monkeypatch.setattr(
            public_endpoint, "_CHAIN_WEIGHTS_FAILURE_BACKOFF_SECONDS", 0.0
        )
        get_weights.side_effect = ChainError("rpc unavailable")

        response = await client.get("/api/v1/public/weights")

        # Serving indefinitely-old chain state would misrepresent it; past the
        # ceiling the endpoint says nothing rather than something wrong.
        assert response.status_code == 503

    async def test_logs_the_exception_type_when_the_read_times_out(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def _never_returns(_netuid: int) -> ChainWeightsSnapshot:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        app.state.chain = SimpleNamespace(get_weights=_never_returns)
        monkeypatch.setattr(public_endpoint, "_CHAIN_WEIGHTS_TIMEOUT_SECONDS", 0.001)

        with caplog.at_level(logging.WARNING, logger=public_endpoint.__name__):
            assert (await client.get("/api/v1/public/weights")).status_code == 503

        # `asyncio.wait_for` raises a bare TimeoutError whose str() is "", which
        # is how this warning used to log an empty message and explain nothing.
        assert any(
            "TimeoutError" in record.getMessage() for record in caplog.records
        ), [r.getMessage() for r in caplog.records]


class TestPublicBenchmarkTimeline:
    async def test_returns_release_events_and_finalized_memory_highs(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The chart is release history, so it has to render retired eras.

        Every shipped contract is on this timeline, v2 included: the endpoint
        dates a release from the rollout that activated it and falls back to the
        changelog epoch. That history is exactly what the bench-version floor
        refuses to write, and exactly what production still holds -- the v2/v3
        rows predate the constraint and are grandfathered by it. Reproducing
        that state is the only way to assert the fallback and the rollout-dated
        release in the same read.
        """
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            first_id = await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.41, 0.42, 0.43],
                details={"bench_version": 2},
                base_time=datetime(2026, 7, 8, tzinfo=UTC),
            )
            second_id = await _seed_k3(
                session_maker,
                miner=_MINER_B,
                composites=[0.71, 0.72, 0.73],
                details={"bench_version": 2},
                base_time=datetime(2026, 7, 9, tzinfo=UTC),
            )
            async with session_maker() as session, session.begin():
                session.add(
                    BenchmarkRollout(
                        rollout_id=uuid4(),
                        from_version=2,
                        desired_version=3,
                        status="activated",
                        cohort_size=5,
                        created_at=datetime(2026, 7, 18, 14, 30, tzinfo=UTC),
                        activated_at=datetime(2026, 7, 18, 16, 0, tzinfo=UTC),
                    )
                )
                for agent_id, recorded_at in (
                    (UUID(first_id), datetime(2026, 7, 8, tzinfo=UTC)),
                    (UUID(second_id), datetime(2026, 7, 9, tzinfo=UTC)),
                ):
                    scores = list(
                        await session.scalars(
                            select(Score).where(Score.agent_id == agent_id)
                        )
                    )
                    for index, score in enumerate(scores):
                        score.created_at = recorded_at + timedelta(minutes=index)
                        score.updated_at = recorded_at + timedelta(minutes=index)
            await _seed_k3(
                session_maker,
                miner="5" + "A" * 47,
                composites=[0.51, 0.52],
                details={"bench_version": 3},
                base_time=datetime(2026, 7, 19, tzinfo=UTC),
            )
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/bench/timeline")

        assert response.status_code == 200
        assert (
            response.headers["cache-control"]
            == "public, max-age=300, stale-while-revalidate=3600"
        )
        body = response.json()
        assert body["metric"] == "memory_mean"
        assert body["score_quorum"] == 3
        # The window follows the changelog, so a new contract must land here
        # without anyone editing a list of versions — this asserts the rule, not
        # a snapshot of today's versions.
        expected_versions = sorted(
            version
            for version in sorted(
                (
                    int(entry["version"])
                    for entry in bench_glossary_data.version_entries()
                    if int(entry["version"])
                    >= public_endpoint._TIMELINE_MIN_BENCH_VERSION
                ),
                reverse=True,
            )[: public_endpoint._TIMELINE_MAX_RELEASES]
        )
        assert [
            release["bench_version"] for release in body["releases"]
        ] == expected_versions
        assert len(expected_versions) == public_endpoint._TIMELINE_MAX_RELEASES
        assert body["releases"][0]["released_at"] == "2026-07-07T00:00:00Z"
        assert body["releases"][1]["released_at"] == "2026-07-18T14:30:00Z"
        assert body["releases"][1]["activated_at"] == "2026-07-18T16:00:00Z"
        assert [point["agent_id"] for point in body["points"]] == [
            first_id,
            second_id,
        ]
        assert [point["memory_mean"] for point in body["points"]] == [0.42, 0.72]

    async def test_a_new_contract_enters_the_window_without_a_code_change(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shipping a bench_version must put it on the timeline by itself.

        This endpoint used to carry the range as a literal, which is why a live
        contract could drive validator weights while the public chart still
        ended a generation earlier. The window now follows the changelog and
        drops the oldest contract rather than growing without bound.
        """
        _install_db(app, session_maker)
        entries = list(bench_glossary_data.version_entries())
        newest = int(entries[0]["version"])
        monkeypatch.setattr(
            bench_glossary_data,
            "version_entries",
            lambda: [
                {
                    "version": newest + 1,
                    "epoch": "2026-08-01",
                    "title": "A contract nobody edited this endpoint for",
                },
                *entries,
            ],
        )

        body = (await client.get("/api/v1/public/bench/timeline")).json()

        versions = [release["bench_version"] for release in body["releases"]]
        assert versions[-1] == newest + 1
        assert len(versions) == public_endpoint._TIMELINE_MAX_RELEASES
        assert versions == sorted(versions)
        assert min(versions) >= public_endpoint._TIMELINE_MIN_BENCH_VERSION


class TestPublicLeaderboard:
    async def test_leaderboard_reports_average_settled_run_cost(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.7, 0.71, 0.72],
                details={"bench_version": _ERA},
            )
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            validator = await session.scalar(
                select(Score.validator_hotkey)
                .where(Score.agent_id == agent_id)
                .limit(1)
            )
            assert validator is not None

            def grant(
                *,
                deadline: datetime,
                status: str,
                chat_cost: int,
                embedding_cost: int,
            ) -> InferenceGrant:
                return InferenceGrant(
                    grant_id=uuid4(),
                    agent_id=agent_id,
                    bench_version=_ERA,
                    validator_hotkey=validator,
                    slot_id="slot-0",
                    ticket_deadline=deadline,
                    expires_at=deadline,
                    status=status,
                    generation=1,
                    allowed_models=["qwen/qwen3-32b"],
                    request_budget=8192,
                    request_count=100,
                    token_budget=25_000_000,
                    prompt_tokens=1000,
                    completion_tokens=100,
                    cost_microusd=chat_cost,
                    embedding_model="perplexity/pplx-embed-v1-0.6b",
                    embedding_profile="dittobench-v8-pplx-embed-v1-0.6b-768-v1",
                    embedding_provider="Perplexity",
                    embedding_dimensions=768,
                    embedding_request_budget=10_000,
                    embedding_request_count=10,
                    embedding_token_budget=5_000_000,
                    embedding_tokens=1000,
                    embedding_cost_microusd=embedding_cost,
                    usage_accounting_version=2,
                    created_at=now - timedelta(hours=2),
                    updated_at=now,
                )

            session.add_all(
                [
                    # Raw active status is stale after the immutable lease end.
                    grant(
                        deadline=now - timedelta(hours=1),
                        status="active",
                        chat_cost=100_000,
                        embedding_cost=10_000,
                    ),
                    # A terminal grant is settled even before its original deadline.
                    grant(
                        deadline=now + timedelta(hours=1),
                        status="exhausted",
                        chat_cost=300_000,
                        embedding_cost=30_000,
                    ),
                    # Live partial work must not pull the board average down.
                    grant(
                        deadline=now + timedelta(hours=2),
                        status="active",
                        chat_cost=900_000,
                        embedding_cost=90_000,
                    ),
                ]
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()
        entry = next(row for row in body["entries"] if row["agent_id"] == str(agent_id))

        assert entry["average_run_cost_microusd"] == 220_000
        assert entry["inference_run_count"] == 2

    async def test_distinguishes_raw_rank_one_from_koth_emissions_champion(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        details = {"bench_version": _ERA, "composite_stderr": 0.03}
        # Both reigns sit under ``KOTH_BAND_DECAY_START_COMPOSITE`` (0.60) so the
        # v6-and-later indifference-band decay leaves the required lead at its
        # unscaled value. What is on trial here is the dethrone decision itself
        # -- that a real 0.05 lead is still short of the statistical bar -- and
        # the decay has its own test; on the live era a 0.80 champion shrinks the
        # band enough to flip this challenger and hide the distinction entirely.
        incumbent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.50, 0.50, 0.50],
            details=details,
            created_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
        raw_leader_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.55, 0.55, 0.55],
            details=details,
            created_at=datetime(2026, 7, 16, tzinfo=UTC),
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.chain = SimpleNamespace(
            get_recent_neurons=AsyncMock(
                return_value=[
                    SimpleNamespace(hotkey=_MINER_A, uid=41),
                    SimpleNamespace(hotkey=_MINER_B, uid=42),
                ]
            )
        )

        body = (await client.get("/api/v1/public/leaderboard")).json()

        assert body["entries"][0]["agent_id"] == raw_leader_id
        assert body["entries"][0]["rank"] == 1
        assert body["emissions"]["raw_leader_agent_id"] == raw_leader_id
        assert body["emissions"]["champion_agent_id"] == incumbent_id
        assert body["emissions"]["margin"] == pytest.approx(0.007)
        assert body["emissions"]["dethrone_z"] == pytest.approx(1.64)
        assert body["emissions"]["band_decay_min_bench_version"] == 6
        assert body["emissions"]["band_decay_start_composite"] == pytest.approx(0.60)
        assert body["emissions"]["band_decay_rate"] == pytest.approx(2.0)
        assert body["emissions"]["rank_shares"] == pytest.approx(
            [0.65, 0.14, 0.10, 0.07, 0.04]
        )
        decision = body["emissions"]["raw_leader_decision"]
        assert decision["challenger_lead"] == pytest.approx(0.05)
        assert decision["required_lead"] == pytest.approx(
            1.64 * (0.03**2 + 0.03**2) ** 0.5
        )
        assert decision["method"] == "unpaired"
        assert decision["dethrones"] is False
        assert body["emissions"]["recipients"] == [
            {
                "role": "champion",
                "agent_id": incumbent_id,
                "miner_hotkey": _MINER_A,
                "raw_rank": 2,
                "share_of_miner_pool": pytest.approx(0.65 / 0.79),
                "shared_seed_confirmations": 0,
            },
            {
                "role": "tail",
                "agent_id": raw_leader_id,
                "miner_hotkey": _MINER_B,
                "raw_rank": 1,
                "share_of_miner_pool": pytest.approx(0.14 / 0.79),
                "shared_seed_confirmations": 0,
            },
        ]

    async def test_leaderboard_surfaces_shared_seed_confirmation_depth(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        champion_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tail_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        # A wave counts only after every emission-set member has the seed.
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.27.0",
                    protocol_version=13,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.90,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in (champion_id, tail_id)
                    for seed in _anchor_seeds(champion_id)
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        mixed = (await client.get("/api/v1/public/leaderboard")).json()
        mixed_entries = {entry["agent_id"]: entry for entry in mixed["entries"]}
        assert mixed["continual_aggregate_active"] is False
        assert mixed_entries[tail_id]["official_composite"] == pytest.approx(0.80)
        assert mixed_entries[tail_id]["aggregate_method"] == "canonical_median"
        assert mixed_entries[tail_id]["completed_wave_count"] == 3
        mixed_recipients = {
            recipient["agent_id"]: recipient
            for recipient in mixed["emissions"]["recipients"]
        }
        assert mixed_recipients[champion_id]["shared_seed_confirmations"] == 0

        async with session_maker() as s, s.begin():
            heartbeat = await s.get(ValidatorHeartbeat, _VALIDATOR_C)
            assert heartbeat is not None
            heartbeat.protocol_version = 14
            heartbeat.software_version = "0.28.0"

        body = (await client.get("/api/v1/public/leaderboard")).json()
        assert body["continual_aggregate_active"] is True
        assert body["continual_aggregate_required_protocol"] == 14
        recipients = {r["agent_id"]: r for r in body["emissions"]["recipients"]}
        entries = {entry["agent_id"]: entry for entry in body["entries"]}
        assert recipients[champion_id]["shared_seed_confirmations"] == 3
        assert entries[champion_id]["composite"] == pytest.approx(0.90)
        assert entries[champion_id]["official_composite"] == pytest.approx(0.90)
        assert entries[champion_id]["aggregate_method"] == "continual_mean"
        assert entries[champion_id]["aggregate_sample_count"] == 6
        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[champion_id]["initial_quorum_composites"] == pytest.approx(
            [0.90, 0.90, 0.90]
        )
        assert entries[champion_id]["completed_wave_composites"] == pytest.approx(
            [0.90, 0.90, 0.90]
        )
        assert entries[tail_id]["composite"] == pytest.approx(0.80)
        assert entries[tail_id]["official_composite"] == pytest.approx(0.85)

    async def test_new_entrant_cannot_remove_retained_samples(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A fresh emission-set member cannot rewrite accepted evidence.

        Regression for the 2026-07-26 incident: three completed waves vanished
        from the public board the moment a newly finalized agent entered the
        top five. ``completed_confirmation_wave_seeds`` intersects over the
        *current* members, so an entrant with no retests empties it board-wide.
        The old global intersection made the entrant's scheduling membership
        erase every sibling's aggregate. Membership now controls future work
        only: each agent permanently averages every seed accepted for it, while
        pairwise KOTH comparisons independently intersect seed identities.

        ``strict`` is pinned to prove the old operator setting cannot restore
        destructive score filtering.
        """
        from ditto.db.models import ContinualRetestSettingsRevision
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        champion_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tail_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            s.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": False,
                        "wave_membership": "strict",
                    },
                    checksum="e" * 64,
                    reason="pin the pre-change fold for this regression",
                    actor="operator@example.com",
                )
            )
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.90,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in (champion_id, tail_id)
                    for seed in _anchor_seeds(champion_id)
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        # The strict pin above is only honoured if the settings cache re-reads.
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        before = (await client.get("/api/v1/public/leaderboard")).json()
        assert before["continual_aggregate_active"] is True
        settled = {entry["agent_id"]: entry for entry in before["entries"]}
        assert settled[champion_id]["completed_wave_count"] == 3
        assert settled[champion_id]["confirmation_seed_depth"] == 3
        assert settled[champion_id]["confirmation_seed_composites"] == pytest.approx(
            [0.90, 0.90, 0.90]
        )

        # A brand-new finalized agent joins the emission set with zero retests.
        entrant_id = await _seed_k3(
            session_maker,
            miner="5Cq" + "z" * 45,
            # Joins the TAIL, below the incumbent: these tests are about a
            # membership change, and only a change that leaves the champion (and
            # therefore the anchor) in place isolates that. A dethroning entrant
            # would also reset the fold, but for the unrelated reason that it
            # re-anchors the seed set -- pinned separately in
            # ``test_a_dethrone_resets_the_fold_but_keeps_the_audit_trail``.
            composites=[0.85, 0.85, 0.85],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        )

        after = (await client.get("/api/v1/public/leaderboard")).json()
        entries = {entry["agent_id"]: entry for entry in after["entries"]}
        # The entrant starts from its quorum median, while existing agents keep
        # every retained sample regardless of the new scheduling membership.
        assert entries[entrant_id]["completed_wave_count"] == 0
        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[champion_id]["completed_wave_composites"] == pytest.approx(
            [0.90, 0.90, 0.90]
        )
        assert entries[champion_id]["aggregate_method"] == "continual_mean"
        assert entries[champion_id]["official_composite"] == pytest.approx(0.90)
        # ...but the append-only audit trail must still be visible.
        assert entries[champion_id]["confirmation_seed_depth"] == 3
        assert entries[champion_id]["confirmation_seed_composites"] == pytest.approx(
            [0.90, 0.90, 0.90]
        )
        assert entries[tail_id]["confirmation_seed_depth"] == 3
        assert entries[entrant_id]["confirmation_seed_depth"] == 0
        assert entries[entrant_id]["confirmation_seed_composites"] == []

    async def test_reports_the_depth_a_non_member_actually_folded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """What the board reports must be what the arithmetic used.

        Production, 2026-07-28: nine agents showed ``completed_wave_count: 0``
        and ``aggregate_method: canonical_median`` while their
        ``official_composite`` had demonstrably moved off the quorum median --
        maybe-v0 sat at rank 4 on a folded 0.8697 against a raw 0.8350, with ten
        wave composites in the payload and a zero beside them. Miners read that
        as "my retests are being discarded"; the runs had in fact all counted.

        The cause is that the fold has two different member sets.
        ``by_seed`` is filtered per agent and never restricted to the emission
        set, so any agent holding the shared seeds folds them. The depth was
        keyed off ``raw_members`` -- the RAW-composite top five -- and an agent
        outside it got a hard zero regardless of what it had averaged. The two
        sets legitimately differ (emissions project from effective composites,
        the intersection from raw), so the depth has to come from the eligible
        seeds themselves, not from membership in either one.
        """
        from ditto.db.models import ContinualRetestSettingsRevision
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        # Six distinct miners: the RAW top five plus one below the cut.
        ladder = [
            (f"5Ck{chr(ord('a') + index)}" + "y" * 44, composite)
            for index, composite in enumerate([0.90, 0.88, 0.86, 0.84, 0.82, 0.80])
        ]
        agent_ids = [
            await _seed_k3(
                session_maker,
                miner=miner,
                composites=[composite] * 3,
                details={"bench_version": _ERA},
                created_at=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=index),
            )
            for index, (miner, composite) in enumerate(ladder)
        ]
        champion_id, *rest = agent_ids
        outsider_id = agent_ids[-1]
        live_seeds = _anchor_seeds(champion_id)

        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            s.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": False,
                        "wave_membership": "participants",
                    },
                    checksum="b" * 64,
                    reason="report folded depth",
                    actor="operator@example.com",
                )
            )
            # Every agent, INCLUDING the one below the raw cut, runs the wave.
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.60,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in agent_ids
                    for seed in live_seeds
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        body = (await client.get("/api/v1/public/leaderboard")).json()
        entries = {entry["agent_id"]: entry for entry in body["entries"]}
        outsider = entries[outsider_id]

        # It folded the wave, so it must say so. This was 0/canonical_median.
        assert len(outsider["completed_wave_composites"]) == 3
        assert outsider["completed_wave_count"] == 3
        assert outsider["aggregate_method"] == "continual_mean"
        assert outsider["aggregate_sample_count"] == 3 + 3
        # And the report agrees with the arithmetic: mean(0.80 x3, 0.60 x3).
        assert outsider["official_composite"] == pytest.approx(0.70)
        # The emission-set members are unaffected.
        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[rest[0]]["completed_wave_count"] == 3

    async def test_cross_reign_history_remains_per_agent_evidence(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rows from a previous reign remain evidence only for their owner.

        Production, 2026-07-27: the board showed every emission recipient at
        ``shared_seed_confirmations: 0`` and "wave pending" while the lane was
        demonstrably running -- one member carried 39 accepted seeds against a
        16-seed-per-reign cap, i.e. three champions' worth of trail.

        The anchor is a pure function of the champion's agent id, so successive
        reigns produce disjoint seed sets. The fold intersected the RAW trail,
        so that deep member was admitted by the ``participants`` predicate on
        rows the current champion never anchored, and then intersected to
        nothing. Board-wide zero, ``official_composite`` reverted to the quorum
        median, and the fold's accumulated evidence silently stopped counting --
        while the lane kept spending validator slots refilling a wave that could
        never complete.

        The reproduction below is that shape exactly: two members holding the
        live anchor's seeds, and a third holding ONLY a foreign anchor's. The
        third is the one that did the damage -- deep enough to look like a
        participant, holding nothing the intersection could keep.
        """
        from ditto.db.models import ContinualRetestSettingsRevision
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        champion_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tail_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        # The lihai analogue: deep trail, none of it on the current anchor.
        stale_only_id = await _seed_k3(
            session_maker,
            miner="5Cq" + "z" * 45,
            composites=[0.70, 0.70, 0.70],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        )
        # A reign this board never had: seeds anchored on some other agent, the
        # way a dethroned champion's trail survives in the append-only table.
        stale_seeds = _anchor_seeds(str(uuid4()), count=8)
        live_seeds = _anchor_seeds(champion_id)
        assert not set(stale_seeds) & set(live_seeds)

        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            s.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": False,
                        "wave_membership": "participants",
                    },
                    checksum="f" * 64,
                    reason="anchor-scoped fold",
                    actor="operator@example.com",
                )
            )
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.60,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in (champion_id, tail_id)
                    for seed in live_seeds
                ]
                + [
                    ConfirmationSeedScore(
                        UUID(stale_only_id),
                        "5V1",
                        seed,
                        0.10,
                        f"stale-{stale_only_id}-{seed}",
                        None,
                    )
                    for seed in stale_seeds
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        body = (await client.get("/api/v1/public/leaderboard")).json()
        entries = {entry["agent_id"]: entry for entry in body["entries"]}

        # The live wave counts. Before the anchor filter the stale-only member
        # emptied the intersection and this was 0 board-wide.
        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[tail_id]["completed_wave_count"] == 3
        assert entries[champion_id]["aggregate_method"] == "continual_mean"
        assert entries[tail_id]["aggregate_method"] == "continual_mean"
        # Equal sample composition: both averaged the same three live seeds.
        assert entries[champion_id]["completed_wave_composites"] == pytest.approx(
            [0.60, 0.60, 0.60]
        )
        assert entries[tail_id]["completed_wave_composites"] == pytest.approx(
            [0.60, 0.60, 0.60]
        )
        # The stale-only member permanently retains its own accepted samples.
        # They affect its mean but cannot become paired evidence against either
        # live member because the seed identities do not intersect.
        assert entries[stale_only_id]["completed_wave_count"] == 8
        assert entries[stale_only_id]["retained_sample_count"] == 8
        assert entries[stale_only_id]["aggregate_method"] == "continual_mean"
        assert entries[stale_only_id]["official_composite"] == pytest.approx(
            (0.70 * 3 + 0.10 * 8) / 11
        )
        # ...but the append-only audit trail is still reported in full.
        assert entries[stale_only_id]["confirmation_seed_depth"] == 8

    async def test_a_dethrone_keeps_the_evidence_the_cohort_already_shares(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A dethrone must NOT throw away what the cohort was measured on.

        The anchor moves with the crown, so the incoming champion contributes a
        disjoint set of NEW seeds. That is the anchor doing its job -- it is the
        growth frontier. It is not a statement that the outgoing reign's seeds
        stopped being valid: a seed IS a dataset, and two agents holding it were
        measured on the same one whatever anchored it. The dethrone test has
        always paired over shared seeds with no anchor filter at all.

        Scoping the fold to the live anchor alone treated a crown change as an
        evidence reset. On the 2026-07-28 board that dropped the fold from ten
        shared seeds to four while the shallowest member of the raw top five
        held sixteen, and it would have thrown away exactly the seeds
        ditto-platform#547 sends the whole cohort to go and cover.

        So the accumulated wave survives the crown, and the audit trail survives
        it too (#485).
        """
        from ditto.db.models import ContinualRetestSettingsRevision
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        champion_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tail_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            s.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": False,
                        "wave_membership": "participants",
                    },
                    checksum="a" * 64,
                    reason="anchor-scoped fold",
                    actor="operator@example.com",
                )
            )
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.60,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in (champion_id, tail_id)
                    for seed in _anchor_seeds(champion_id)
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        before = (await client.get("/api/v1/public/leaderboard")).json()
        settled = {entry["agent_id"]: entry for entry in before["entries"]}
        assert settled[champion_id]["completed_wave_count"] == 3
        assert settled[champion_id]["aggregate_method"] == "continual_mean"

        # Takes the crown outright: 0.95 clears 0.90 by far more than the margin.
        usurper_id = await _seed_k3(
            session_maker,
            miner="5Cq" + "z" * 45,
            composites=[0.95, 0.95, 0.95],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        )

        after = (await client.get("/api/v1/public/leaderboard")).json()
        entries = {entry["agent_id"]: entry for entry in after["entries"]}
        assert entries[usurper_id]["rank"] == 1
        # The crown moved; the shared evidence did not. Both agents that ran the
        # wave keep all three seeds and stay on the continual mean.
        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[champion_id]["aggregate_method"] == "continual_mean"
        assert entries[champion_id]["official_composite"] == pytest.approx(0.75)
        assert entries[tail_id]["completed_wave_count"] == 3
        # The usurper has run none of them, so it stays on the quorum median --
        # and, holding nothing anyone else holds, it cannot empty the wave.
        assert entries[usurper_id]["completed_wave_count"] == 0
        assert entries[usurper_id]["aggregate_method"] == "canonical_median"
        # The accepted rows were never deleted.
        assert entries[champion_id]["confirmation_seed_depth"] == 3
        assert entries[tail_id]["confirmation_seed_depth"] == 3

    async def test_participants_membership_keeps_the_fold_on_the_continual_mean(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The other half of the 03:56Z incident: the WEIGHT side, not display.

        #485 made the audit trail survive a membership change. It deliberately
        left ``official_composite`` alone, so an entrant with no retests still
        knocks every agent off the continual mean and back onto the three-score
        quorum median -- and that aggregate is what validators weight.

        With ``wave_membership="participants"`` the zero-depth entrant no longer
        empties the intersection, so the agents that actually ran the waves keep
        their accumulated estimator. The entrant itself still gets the canonical
        median, exactly like every agent outside the emission set.
        """
        from ditto.db.models import ContinualRetestSettingsRevision
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        champion_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tail_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            s.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": False,
                        "wave_membership": "participants",
                    },
                    checksum="c" * 64,
                    reason="keep retest evidence across a membership change",
                    actor="operator@example.com",
                )
            )
            # 0.60 on every wave seed, well below the 0.90 quorum median, so the
            # continual mean is unmistakably distinguishable from the fallback:
            # mean(0.90, 0.90, 0.90, 0.60, 0.60, 0.60) = 0.75.
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.60,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in (champion_id, tail_id)
                    for seed in _anchor_seeds(champion_id)
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        entrant_id = await _seed_k3(
            session_maker,
            miner="5Cq" + "z" * 45,
            # Joins the TAIL, below the incumbent: these tests are about a
            # membership change, and only a change that leaves the champion (and
            # therefore the anchor) in place isolates that. A dethroning entrant
            # would also reset the fold, but for the unrelated reason that it
            # re-anchors the seed set -- pinned separately in
            # ``test_a_dethrone_resets_the_fold_but_keeps_the_audit_trail``.
            composites=[0.85, 0.85, 0.85],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        )

        body = (await client.get("/api/v1/public/leaderboard")).json()
        entries = {entry["agent_id"]: entry for entry in body["entries"]}

        # The evidence survives the entrant, on the FOLD side this time.
        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[champion_id]["aggregate_method"] == "continual_mean"
        assert entries[champion_id]["official_composite"] == pytest.approx(0.75)
        assert entries[tail_id]["completed_wave_count"] == 3
        # Equal sample composition: both agents averaged the same three seeds.
        assert entries[champion_id]["completed_wave_composites"] == pytest.approx(
            entries[tail_id]["completed_wave_composites"]
        )
        # The entrant has run nothing, so it stays on the canonical median --
        # the same estimator every agent outside the emission set already uses.
        assert entries[entrant_id]["completed_wave_count"] == 0
        assert entries[entrant_id]["aggregate_method"] == "canonical_median"
        assert entries[entrant_id]["official_composite"] == pytest.approx(0.85)
        # And the raw audit trail from #485 is still there underneath.
        assert entries[champion_id]["confirmation_seed_depth"] == 3

    async def test_rank_follows_official_composite_not_composite(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pin the invariant a consumer actually needs: sorting the board by the
        field ``rank`` is derived from must reproduce ``rank``.

        Nothing caught this before. The board is ranked by ``official_composite``
        (the continual mean), but ``composite`` is the field that *looks* like
        the score, and on 2026-07-26 the production board had a champion whose
        ``composite`` was only 4th best. An operator reading ``composite`` as
        "the score" concluded the wrong agent was winning and nearly moved 65%
        of miner emissions on it.

        So this deliberately builds a board where the two orderings INVERT, and
        asserts three things: ``rank`` tracks ``official_composite``, it does
        *not* track ``composite``, and ``raw_rank`` on the emission recipients
        tracks ``composite`` (which is what that field has always meant).
        """
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        # Leads on the single-quorum median (0.90) ...
        median_leader_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        # ... but trails once the completed waves land.
        mean_leader_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        wave_scores = {median_leader_id: 0.60, mean_leader_id: 0.95}
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        value,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id, value in wave_scores.items()
                    for seed in _anchor_seeds(median_leader_id)
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()
        assert body["continual_aggregate_active"] is True
        entries = body["entries"]
        by_id = {entry["agent_id"]: entry for entry in entries}

        # mean(0.90, 0.90, 0.90, 0.60, 0.60, 0.60) = 0.75
        assert by_id[median_leader_id]["composite"] == pytest.approx(0.90)
        assert by_id[median_leader_id]["official_composite"] == pytest.approx(0.75)
        # mean(0.80, 0.80, 0.80, 0.95, 0.95, 0.95) = 0.875
        assert by_id[mean_leader_id]["composite"] == pytest.approx(0.80)
        assert by_id[mean_leader_id]["official_composite"] == pytest.approx(0.875)
        assert by_id[mean_leader_id]["aggregate_method"] == "continual_mean"

        # The two orderings genuinely disagree, or this test proves nothing.
        assert [
            e["agent_id"] for e in sorted(entries, key=lambda e: -e["composite"])
        ] != [
            e["agent_id"]
            for e in sorted(entries, key=lambda e: -e["official_composite"])
        ]

        # THE INVARIANT: sorting by official_composite reproduces rank exactly.
        assert [e["rank"] for e in entries] == sorted(e["rank"] for e in entries)
        assert [
            e["agent_id"]
            for e in sorted(
                entries, key=lambda e: (-e["official_composite"], e["rank"])
            )
        ] == [e["agent_id"] for e in sorted(entries, key=lambda e: e["rank"])]
        assert by_id[mean_leader_id]["rank"] == 1
        assert by_id[median_leader_id]["rank"] == 2

        # And raw_rank is the OTHER ordering on purpose: by canonical median.
        # The champion here carries raw_rank 2 while holding board rank 1 --
        # the exact shape that read as a bug in production.
        recipients = {r["agent_id"]: r for r in body["emissions"]["recipients"]}
        assert recipients[mean_leader_id]["role"] == "champion"
        assert recipients[mean_leader_id]["raw_rank"] == 2
        assert recipients[median_leader_id]["raw_rank"] == 1

    async def test_the_shipped_default_survives_a_membership_change(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pins ``participants`` as the DEFAULT, not merely as an option.

        The companion test above proves the mode works when an operator selects
        it. This one writes a revision that never mentions ``wave_membership``
        at all, so the fold runs on whatever ships. It is deliberately the
        03:56Z scenario: if the default were ever moved back to ``strict``, the
        zero-depth entrant would empty the intersection and this goes red with
        ``completed_wave_count == 0`` and a ``canonical_median`` fallback --
        which is precisely the regression that reverted every agent's
        ``official_composite`` while v7 was driving validator weights.
        """
        from ditto.db.models import ContinualRetestSettingsRevision
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        champion_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90, 0.90, 0.90],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tail_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.80, 0.80, 0.80],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            s.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    # No ``wave_membership`` key: this is the whole point.
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": False,
                    },
                    checksum="d" * 64,
                    reason="defaults only",
                    actor="operator@example.com",
                )
            )
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent_id),
                        "5V1",
                        seed,
                        0.60,
                        f"r{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in (champion_id, tail_id)
                    for seed in _anchor_seeds(champion_id)
                ],
                bench_version=_ERA,
                created_at=datetime.now(UTC),
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        await _seed_k3(
            session_maker,
            miner="5Cq" + "z" * 45,
            # Joins the TAIL, below the incumbent: these tests are about a
            # membership change, and only a change that leaves the champion (and
            # therefore the anchor) in place isolates that. A dethroning entrant
            # would also reset the fold, but for the unrelated reason that it
            # re-anchors the seed set -- pinned separately in
            # ``test_a_dethrone_resets_the_fold_but_keeps_the_audit_trail``.
            composites=[0.85, 0.85, 0.85],
            details={"bench_version": _ERA},
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        )

        body = (await client.get("/api/v1/public/leaderboard")).json()
        entries = {entry["agent_id"]: entry for entry in body["entries"]}

        assert entries[champion_id]["completed_wave_count"] == 3
        assert entries[champion_id]["aggregate_method"] == "continual_mean"
        assert entries[champion_id]["official_composite"] == pytest.approx(0.75)
        assert entries[tail_id]["aggregate_method"] == "continual_mean"

    async def test_marks_deregistered_scores_retained_but_emission_ineligible(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
            details={"bench_version": _ERA},
        )
        await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.6, 0.7, 0.8],
            details={"bench_version": _ERA},
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.chain = SimpleNamespace(
            get_recent_neurons=AsyncMock(
                return_value=[SimpleNamespace(hotkey=_MINER_B, uid=42)]
            )
        )

        body = (await client.get("/api/v1/public/leaderboard")).json()

        by_miner = {e["miner_hotkey"]: e for e in body["entries"]}
        assert by_miner[_MINER_A]["registered"] is False
        assert by_miner[_MINER_A]["miner_uid"] is None
        assert by_miner[_MINER_A]["emission_eligible"] is False
        assert by_miner[_MINER_A]["finalized"] is True
        assert by_miner[_MINER_A]["score_count"] == 3
        assert by_miner[_MINER_B]["registered"] is True
        assert by_miner[_MINER_B]["miner_uid"] == 42
        assert by_miner[_MINER_B]["emission_eligible"] is True

    async def test_chain_error_keeps_leaderboard_available_with_unknown_registration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
            details={"bench_version": _ERA},
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        app.state.chain = SimpleNamespace(
            get_recent_neurons=AsyncMock(side_effect=ChainError("pylon unavailable"))
        )

        response = await client.get("/api/v1/public/leaderboard")

        assert response.status_code == 200
        entry = response.json()["entries"][0]
        assert entry["registered"] is None
        assert entry["emission_eligible"] is None

    async def test_chain_timeout_keeps_leaderboard_available_with_unknown_registration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
            details={"bench_version": _ERA},
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        async def _never_returns(_netuid: int) -> list[object]:
            await asyncio.Event().wait()
            return []

        app.state.chain = SimpleNamespace(get_recent_neurons=_never_returns)
        monkeypatch.setattr(
            public_endpoint, "_REGISTRATION_LOOKUP_TIMEOUT_SECONDS", 0.001
        )

        response = await client.get("/api/v1/public/leaderboard")

        assert response.status_code == 200
        entry = response.json()["entries"][0]
        assert entry["registered"] is None
        assert entry["emission_eligible"] is None

    async def test_failed_registration_refresh_keeps_the_last_known_good_mapping(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
            details={"bench_version": _ERA},
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        get_recent_neurons = AsyncMock(
            return_value=[SimpleNamespace(hotkey=_MINER_A, uid=42)]
        )
        app.state.chain = SimpleNamespace(get_recent_neurons=get_recent_neurons)
        # The snapshot bakes its expiry in at write time, so the TTL has to be
        # zeroed before the first read for the second one to attempt a refresh.
        monkeypatch.setattr(public_endpoint, "_REGISTRATION_CACHE_TTL_SECONDS", 0.0)

        first = (await client.get("/api/v1/public/leaderboard")).json()
        assert first["entries"][0]["registered"] is True
        assert first["registration_stale"] is False

        get_recent_neurons.side_effect = ChainError("pylon unavailable")

        body = (await client.get("/api/v1/public/leaderboard")).json()

        # The row keeps its real registration rather than flipping every row on
        # the board to "unknown" for one poll and back on the next.
        entry = body["entries"][0]
        assert entry["registered"] is True
        assert entry["miner_uid"] == 42
        assert body["registration_stale"] is True

    async def test_registration_becomes_unknown_once_the_last_read_is_too_old(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
            details={"bench_version": _ERA},
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        get_recent_neurons = AsyncMock(
            return_value=[SimpleNamespace(hotkey=_MINER_A, uid=42)]
        )
        app.state.chain = SimpleNamespace(get_recent_neurons=get_recent_neurons)
        monkeypatch.setattr(public_endpoint, "_REGISTRATION_CACHE_TTL_SECONDS", 0.0)
        assert (await client.get("/api/v1/public/leaderboard")).status_code == 200

        monkeypatch.setattr(public_endpoint, "_REGISTRATION_MAX_STALE_SECONDS", -1.0)
        get_recent_neurons.side_effect = ChainError("pylon unavailable")

        body = (await client.get("/api/v1/public/leaderboard")).json()

        assert body["entries"][0]["registered"] is None
        assert body["registration_stale"] is False

    async def test_logs_the_exception_type_when_registration_read_times_out(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
            details={"bench_version": _ERA},
        )
        _install_db(app, session_maker)

        async def _never_returns(_netuid: int) -> list[object]:
            await asyncio.Event().wait()
            return []

        app.state.chain = SimpleNamespace(get_recent_neurons=_never_returns)
        monkeypatch.setattr(
            public_endpoint, "_REGISTRATION_LOOKUP_TIMEOUT_SECONDS", 0.001
        )

        with caplog.at_level(logging.WARNING, logger=public_endpoint.__name__):
            assert (await client.get("/api/v1/public/leaderboard")).status_code == 200

        # `asyncio.timeout` raises a bare TimeoutError whose str() is "": this
        # warning used to fire hundreds of times a day with an empty message.
        assert any(
            "registration read failed" in record.getMessage()
            and "TimeoutError" in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    async def test_includes_pre_quorum_scores_as_provisional_feedback(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.6, 0.8],
            status=AgentStatus.EVALUATING,
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()

        assert body["count"] == 1
        entry = body["entries"][0]
        assert entry["miner_hotkey"] == _MINER_A
        assert entry["composite"] == pytest.approx(0.7)
        assert entry["tool_mean"] == pytest.approx(0.7)
        assert entry["memory_mean"] == pytest.approx(0.7)
        assert entry["finalized"] is False
        assert entry["score_count"] == 2
        assert entry["score_quorum"] == 3
        assert entry["bench_version"] == _ERA

    async def test_provisional_overlay_gives_one_row_per_coldkey_not_per_hotkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The overlay must group the way the emission ledger does.

        ``list_eligible_ledger`` ranks one row per payment-time coldkey, so a
        coldkey funding several hotkeys holds one board position. Keyed on the
        hotkey, the provisional overlay showed an owner a second row — and even
        showed a provisional row beside its own finalized one.
        """
        shared_coldkey = "5SharedProvisionalColdkey"
        settled_coldkey = "5SettledColdkey"
        best_provisional = await _seed_k3(
            session_maker,
            miner="5" + "P" * 47,
            composites=[0.80, 0.80],
            status=AgentStatus.EVALUATING,
        )
        sibling_provisional = await _seed_k3(
            session_maker,
            miner="5" + "Q" * 47,
            composites=[0.75],
            status=AgentStatus.EVALUATING,
        )
        finalized = await _seed_k3(
            session_maker,
            miner="5" + "R" * 47,
            composites=[0.70, 0.70, 0.70],
        )
        # Same owner as ``finalized``, and scoring higher: without owner
        # grouping this outranks its own settled row on the public board.
        shadow_provisional = await _seed_k3(
            session_maker,
            miner="5" + "S" * 47,
            composites=[0.85],
            status=AgentStatus.EVALUATING,
        )
        for index, (agent_id, hotkey, coldkey) in enumerate(
            (
                (best_provisional, "5" + "P" * 47, shared_coldkey),
                (sibling_provisional, "5" + "Q" * 47, shared_coldkey),
                (finalized, "5" + "R" * 47, settled_coldkey),
                (shadow_provisional, "5" + "S" * 47, settled_coldkey),
            ),
            start=1,
        ):
            await _seed_payment(
                session_maker,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                index=index,
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()

        listed = [entry["agent_id"] for entry in body["entries"]]
        assert listed == [finalized, best_provisional]
        assert body["count"] == 2

    async def test_owner_family_keeps_hidden_generations_visible_without_ranks(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        coldkey = "5SharedFinalizedFamilyColdkey"
        representative = await _seed_k3(
            session_maker,
            miner="5" + "T" * 47,
            composites=[0.95, 0.96, 0.97],
            created_at=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        )
        hidden_generation = await _seed_k3(
            session_maker,
            miner="5" + "U" * 47,
            composites=[0.90, 0.91, 0.92],
            created_at=datetime(2026, 6, 8, 13, 0, tzinfo=UTC),
        )
        await _seed_payment(
            session_maker,
            agent_id=representative,
            miner_hotkey="5" + "T" * 47,
            miner_coldkey=coldkey,
            index=41,
        )
        await _seed_payment(
            session_maker,
            agent_id=hidden_generation,
            miner_hotkey="5" + "U" * 47,
            miner_coldkey=coldkey,
            index=42,
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        board = (await client.get("/api/v1/public/leaderboard")).json()

        assert board["count"] == 1
        entry = board["entries"][0]
        assert entry["agent_id"] == representative
        family = entry["submission_family"]
        assert family["member_count"] == 2
        assert family["selection_rule"] == "best_official_score_per_payment_owner"
        assert [member["agent_id"] for member in family["members"]] == [
            representative,
            hidden_generation,
        ]
        assert [member["representative"] for member in family["members"]] == [
            True,
            False,
        ]

        pipeline = (
            await client.get(f"/api/v1/public/agent/{hidden_generation}/pipeline")
        ).json()
        assert pipeline["submission_family"] == family

    async def test_attested_owner_family_keeps_linked_coldkeys_visible(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        representative_hotkey = "5" + "V" * 47
        hidden_hotkey = "5" + "W" * 47
        representative = await _seed_k3(
            session_maker,
            miner=representative_hotkey,
            composites=[0.95, 0.96, 0.97],
            created_at=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
        )
        hidden = await _seed_k3(
            session_maker,
            miner=hidden_hotkey,
            composites=[0.90, 0.91, 0.92],
            created_at=datetime(2026, 6, 8, 13, 0, tzinfo=UTC),
        )
        await _seed_payment(
            session_maker,
            agent_id=representative,
            miner_hotkey=representative_hotkey,
            miner_coldkey="test-coldkey-a",
            index=43,
        )
        await _seed_payment(
            session_maker,
            agent_id=hidden,
            miner_hotkey=hidden_hotkey,
            miner_coldkey="test-coldkey-b",
            index=44,
        )
        async with session_maker() as session, session.begin():
            session.add(
                OwnerAttestation(
                    netuid=expected_netuid(),
                    hotkey_lo=representative_hotkey,
                    hotkey_hi=hidden_hotkey,
                    nonce=uuid4(),
                    issued_at=datetime.now(UTC),
                    lo_key_kind="hotkey",
                    lo_signer=representative_hotkey,
                    lo_signature="a" * 128,
                    hi_key_kind="hotkey",
                    hi_signer=hidden_hotkey,
                    hi_signature="b" * 128,
                )
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        board = (await client.get("/api/v1/public/leaderboard")).json()

        assert board["count"] == 1
        family = board["entries"][0]["submission_family"]
        assert family["member_count"] == 2
        assert [member["agent_id"] for member in family["members"]] == [
            representative,
            hidden,
        ]
        hidden_pipeline = (
            await client.get(f"/api/v1/public/agent/{hidden}/pipeline")
        ).json()
        assert hidden_pipeline["submission_family"] == family

    async def test_open_rollout_exposes_settled_and_rollout_state_per_entry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Mid-rollout, every entry carries the settled median of the era in
        force plus the next era's settlement state (median so far + score
        count). With the temporary authority pin, even a complete quorum on the
        target version stays on the settled median until the rollout
        activates."""
        await _activate_era(session_maker)
        flipped_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.80, 0.80, 0.80],
        )
        partial_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.85, 0.85, 0.85],
        )
        async with session_maker() as s, s.begin():
            s.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_ERA,
                    desired_version=_NEXT_ERA,
                    status="collecting",
                    cohort_size=5,
                    created_at=datetime.now(UTC),
                )
            )
            for i, composite in enumerate([0.90, 0.92, 0.94]):
                await upsert_score(
                    s,
                    agent_id=UUID(flipped_id),
                    validator_hotkey=f"5Validator{i}Flipped",
                    bench_version=_NEXT_ERA,
                    run_id=f"v3_run_{i}",
                    seed=1,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=500,
                    n=110,
                    generated_at=datetime(2026, 7, 18, 12, i, tzinfo=UTC),
                    signature="ab" * 64,
                )
            await upsert_score(
                s,
                agent_id=UUID(partial_id),
                validator_hotkey="5Validator0Partial",
                bench_version=_NEXT_ERA,
                run_id="v3_run_partial",
                seed=1,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=500,
                n=110,
                generated_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
                signature="ab" * 64,
            )
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()

        assert body["active_bench_version"] == _ERA
        assert body["desired_bench_version"] == _NEXT_ERA
        assert body["available_bench_versions"] == [_NEXT_ERA, _ERA]
        by_agent = {e["agent_id"]: e for e in body["entries"]}
        flipped = by_agent[flipped_id]
        assert flipped["bench_version"] == _ERA
        assert flipped["composite"] == pytest.approx(0.80)
        assert flipped["settled_composite"] == pytest.approx(0.80)
        assert flipped["rollout_composite"] == pytest.approx(0.92)
        assert flipped["rollout_score_count"] == 3
        partial = by_agent[partial_id]
        assert partial["bench_version"] == _ERA
        assert partial["composite"] == pytest.approx(0.85)
        assert partial["settled_composite"] == pytest.approx(0.85)
        assert partial["rollout_composite"] == pytest.approx(0.5)
        assert partial["rollout_score_count"] == 1

    async def test_rollout_state_is_null_without_an_open_rollout(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.7, 0.8, 0.9],
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()

        entry = body["entries"][0]
        assert entry["settled_composite"] is None
        assert entry["rollout_composite"] is None
        assert entry["rollout_score_count"] is None

    async def test_finalized_miner_supersedes_partial_submission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6],
        )
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.99],
            status=AgentStatus.EVALUATING,
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()

        assert body["count"] == 1
        entry = body["entries"][0]
        assert entry["composite"] == pytest.approx(0.5)
        assert entry["finalized"] is True
        assert entry["score_count"] == 3

    async def test_ranks_by_composite_and_exposes_aggregates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_scored(
            session_maker, miner=_MINER_A, composite=0.4, tool_mean=0.5, memory_mean=0.3
        )
        await _seed_scored(
            session_maker,
            miner=_MINER_B,
            composite=0.9,
            tool_mean=0.95,
            memory_mean=0.8,
        )
        # Held (suspected copy) must not surface.
        await _seed_scored(
            session_maker,
            miner="5HeldMinerXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            composite=0.99,
            tool_mean=0.99,
            memory_mean=0.99,
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        resp = await client.get("/api/v1/public/leaderboard")
        assert resp.status_code == 200
        assert (
            resp.headers["Cache-Control"]
            == "public, max-age=30, stale-while-revalidate=120"
        )
        body = resp.json()
        assert body["selection_mode"] == "authoritative"
        assert body["active_bench_version"] == _ERA
        assert body["desired_bench_version"] == _ERA
        assert body["current_bench_version"] == _ERA
        assert body["available_bench_versions"] == [_ERA]
        assert body["count"] == 2
        assert [e["rank"] for e in body["entries"]] == [1, 2]
        assert [e["miner_hotkey"] for e in body["entries"]] == [_MINER_B, _MINER_A]
        assert all(e["finalized"] is False for e in body["entries"])
        assert all(e["score_count"] == 1 for e in body["entries"])
        top = body["entries"][0]
        assert top["agent_name"] == "agent"
        assert top["agent_version"] is None
        assert top["composite"] == pytest.approx(0.9)
        assert top["tool_mean"] == pytest.approx(0.95)
        assert top["memory_mean"] == pytest.approx(0.8)

        historical = (
            await client.get(f"/api/v1/public/leaderboard?bench_version={_ERA}")
        ).json()
        assert historical["selection_mode"] == "historical"
        assert historical["entries"] == body["entries"]
        assert historical["emissions"] is None
        assert historical["available_bench_versions"] == [_ERA]

    async def test_settled_bench_version_board_caches_longer_than_the_live_one(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)

        live = await client.get("/api/v1/public/leaderboard")
        # "In play" and "settled" are the subject here, not any particular
        # generation. With no rollout on record the ledger's authority is the
        # floor, so ``_ERA`` is the version still in play and ``_PREV_ERA`` --
        # the newest retired one -- is the finished work behind it.
        pinned_live = await client.get(
            f"/api/v1/public/leaderboard?bench_version={_ERA}"
        )
        settled = await client.get(
            f"/api/v1/public/leaderboard?bench_version={_PREV_ERA}"
        )

        live_window = "public, max-age=30, stale-while-revalidate=120"
        assert live.headers["Cache-Control"] == live_window
        # The version still in play is not history, even pinned explicitly.
        assert pinned_live.headers["Cache-Control"] == live_window
        # A version the rollout has moved past is finished work, so a reload of
        # the timeline's per-contract boards costs no requests.
        assert (
            settled.headers["Cache-Control"]
            == "public, max-age=3600, stale-while-revalidate=86400"
        )

    async def test_exposes_advisory_calibration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # P5: the advisory Brier calibration telemetry surfaces as an unscored
        # column; a run without it (or with a malformed value) shows null.
        await _seed_scored(
            session_maker,
            miner=_MINER_A,
            composite=0.7,
            tool_mean=0.7,
            memory_mean=0.7,
            details={"calibration_brier": 0.12, "calibration_n": 34},
        )
        await _seed_scored(
            session_maker,
            miner=_MINER_B,
            composite=0.6,
            tool_mean=0.6,
            memory_mean=0.6,
            details={"calibration_brier": 7.5},  # out of range → dropped
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/leaderboard")).json()
        by_miner = {e["miner_hotkey"]: e for e in body["entries"]}
        assert by_miner[_MINER_A]["calibration_brier"] == pytest.approx(0.12)
        assert by_miner[_MINER_A]["calibration_n"] == 34
        assert by_miner[_MINER_B]["calibration_brier"] is None
        assert by_miner[_MINER_B]["calibration_n"] is None

    async def test_never_leaks_integrity_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Seed a run whose details carry the raw per-case answer key so we can
        # assert it is redacted out, not merely absent because it was never set.
        details = {
            "bench_version": _ERA,
            "per_case": [
                {
                    "kind": "tool",
                    "category": "web_search",
                    "score": 0.6,
                    "correct": False,
                    "latency_ms": 3382,
                    "notes": ["1 extra/unexpected tool call(s)"],
                    "expected": ["search_web"],
                    "called": ["search_web", "search_web"],
                    "case_id": "web_search-8860569897825046057-0001",
                },
            ],
        }
        await _seed_scored(
            session_maker,
            miner=_MINER_A,
            composite=0.4,
            tool_mean=0.5,
            memory_mean=0.3,
            details=details,
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        resp = await client.get("/api/v1/public/leaderboard")
        entry = resp.json()["entries"][0]
        # agent_id is deliberately exposed (already public via /submissions and the
        # per-agent drill-in endpoints) so the dashboard can link a row to its k=3
        # record; the seed and the per-validator/artifact identifiers stay hidden.
        assert "agent_id" in entry
        for leaked in ("signature", "sha256", "validator_hotkey", "seed"):
            assert leaked not in entry
        # The answer key must appear NOWHERE in the whole response, even nested
        # inside the redacted per-case results. Check the quoted JSON keys (so a
        # note like "unexpected tool call" doesn't false-match "expected") plus
        # the expected/called tool token itself.
        raw = resp.text
        for answer_key in ('"expected"', '"called"', '"case_id"', "search_web"):
            assert answer_key not in raw
        # …but the safe, redacted per-case view IS surfaced for analysis.
        cases = entry["case_results"]
        assert cases and cases[0]["category"] == "web_search"
        assert cases[0]["score"] == pytest.approx(0.6)
        assert cases[0]["correct"] is False
        assert set(cases[0]).issubset(
            {"category", "kind", "score", "correct", "latency_ms", "notes"}
        )

    async def test_empty_ledger(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        resp = await client.get("/api/v1/public/leaderboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["entries"] == []

    async def test_no_auth_required(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        # No X-Validator-Hotkey header, no chain override — must still succeed.
        resp = await client.get("/api/v1/public/leaderboard")
        assert resp.status_code == 200


class TestPublicHealth:
    async def test_counts_latency_and_window(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        # Two scored miners, latencies 400 + 800 => avg 600. The signed report
        # timestamps are deliberately stale: public activity must use when the
        # platform recorded each score, not validator-controlled provenance.
        await _seed_scored(
            session_maker,
            miner=_MINER_A,
            composite=0.4,
            tool_mean=0.5,
            memory_mean=0.3,
            median_ms=400,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            recorded_at=now - timedelta(minutes=5),
        )
        await _seed_scored(
            session_maker,
            miner=_MINER_B,
            composite=0.9,
            tool_mean=0.95,
            memory_mean=0.8,
            median_ms=800,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            recorded_at=now - timedelta(days=2),  # outside the 24h window
        )
        # A third miner who submitted but has not been scored yet.
        await _seed_agent(
            session_maker,
            miner="5CFn5zVKp6taKY8T39M92cWWpsCXBQym37waFAtiKmZmznu9",
            status=AgentStatus.UPLOADED,
        )
        _install_db(app, session_maker)

        resp = await client.get("/api/v1/public/health")
        assert resp.status_code == 200
        assert (
            resp.headers["Cache-Control"]
            == "public, max-age=30, stale-while-revalidate=120"
        )
        body = resp.json()
        assert body["miners"] == 3
        assert body["scored_miners"] == 2
        assert body["scored_agents"] == 2
        assert body["total_scores"] == 2
        assert body["scores_24h"] == 1  # only MINER_A is within 24h
        assert body["avg_latency_ms"] == 600
        # last_scored_at is the newest platform write (MINER_A, ~5 min ago).
        last = datetime.fromisoformat(body["last_scored_at"])
        assert abs((now - last).total_seconds()) < 3600

    async def test_orphan_scored_agent_not_counted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A scored-STATUS agent with no score row (a stray/hand-edited state)
        # must not inflate the scored counts — they require a real score row so
        # health can never contradict the leaderboard.
        await _seed_scored(
            session_maker,
            miner=_MINER_A,
            composite=0.5,
            tool_mean=0.6,
            memory_mean=0.4,
            generated_at=datetime.now(UTC),
        )
        await _seed_agent(
            session_maker, miner=_MINER_B, status=AgentStatus.SCORED
        )  # scored status, but no score row
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/health")).json()
        assert body["miners"] == 2  # both submitted
        assert body["scored_miners"] == 1  # only MINER_A is score-backed
        assert body["scored_agents"] == 1

    async def test_held_agent_not_counted_as_scored(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A held (ATH review) agent has a score but is not eligible: it counts
        # toward total miners but not scored_miners/scored_agents.
        await _seed_scored(
            session_maker,
            miner=_MINER_A,
            composite=0.99,
            tool_mean=0.99,
            memory_mean=0.99,
            status=AgentStatus.ATH_PENDING_REVIEW,
            generated_at=datetime.now(UTC),
        )
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/health")).json()
        assert body["miners"] == 1
        assert body["scored_miners"] == 0
        assert body["scored_agents"] == 0

    async def test_empty(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        resp = await client.get("/api/v1/public/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "generated_at": body["generated_at"],
            "miners": 0,
            "scored_miners": 0,
            "scored_agents": 0,
            "last_scored_at": None,
            "total_scores": 0,
            "scores_24h": 0,
            "avg_latency_ms": None,
        }


def _liveness_row(
    now: datetime, *, protocol_version: int, scorer: dict | None
) -> SimpleNamespace:
    """One stored heartbeat row, healthy in every respect except the scorer."""
    capabilities = _scorer_capabilities(now, versions=[2, 3])
    if scorer is None:
        capabilities.pop("scorer_benchmarks")
    else:
        capabilities["scorer_benchmarks"] = scorer
    component = {
        "health": "healthy",
        "required": True,
        "observed_at": int(now.timestamp()),
        "ready": True,
    }
    return SimpleNamespace(
        validator_hotkey=_VALIDATOR_C,
        software_version="0.29.6",
        protocol_version=protocol_version,
        state="idle",
        active_agent_id=None,
        system_metrics={
            "collected_at": int(now.timestamp()),
            "cpu_percent": 10,
            "memory_percent": 20,
            "disk_percent": 30,
            "docker": {
                "status": "healthy",
                "running_containers": 6,
                "unhealthy_containers": 0,
            },
        },
        benchmark_progress=None,
        benchmark_capacity=None,
        capabilities=capabilities,
        stack={
            "mode": "managed",
            "compose_schema": 2,
            "release_descriptor_digest": "sha256:" + "c" * 64,
            "components": {
                name: {
                    "image_digest": "sha256:" + "d" * 64,
                    "provenance": "signed_descriptor",
                }
                for name in (
                    "ditto_subnet",
                    "dittobench_api",
                    "sandbox_docker",
                    "model_relay",
                    "pylon",
                    "ollama",
                )
            },
        },
        stack_health={
            name: dict(component)
            for name in (
                "ditto_subnet",
                "dittobench_api",
                "sandbox_docker",
                "model_relay",
                "pylon",
                "ollama",
            )
        },
        first_seen_at=now - timedelta(days=1),
        reported_at=now,
        seen_at=now,
    )


class TestScorerLivenessSurfacing:
    """A validator whose scorer is not serving must not read like a warning.

    Both incidents that produced the probe showed the same thing on the fleet
    view: a validator that could not complete a single lease, rendered beside
    every validator that merely had a full disk.
    """

    def _entry(
        self,
        *,
        protocol_version: int,
        scorer: dict | None,
        active_bench_version: int = LEGACY_BENCH_VERSION,
    ):
        """One entry, judged at the legacy era so only liveness can colour it.

        The bench-capability gate exempts the legacy version exactly as ticket
        issuance does, which keeps these cases about the scorer probe: every row
        here advertises v2, so none of them can be failed for the wrong reason.
        """
        now = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
        response = public_endpoint._validator_heartbeats_response(
            rows=[_liveness_row(now, protocol_version=protocol_version, scorer=scorer)],
            assignments=[],
            active_work=[],
            orphaned_leases=[],
            now=now,
            active_bench_version=active_bench_version,
            slot_settings=SLOT_SETTINGS_DEFAULT,
        )
        return response.validators[0]

    def test_a_scorer_that_never_answered_reads_critical(self) -> None:
        """The TAO.com sidecar: 404 on every probe, previously ``warning``."""
        entry = self._entry(
            protocol_version=15,
            scorer={
                "status": "legacy_v2",
                "supported_bench_versions": [2],
                "probe": {
                    "outcome": "http_error",
                    "observed_at": 1_784_000_000,
                    "http_status": 404,
                    "consecutive_failures": 97,
                },
            },
        )

        assert entry.scorer_liveness == "not_serving"
        assert entry.health == "critical"
        assert entry.health_reasons == ["scorer not serving: http 404 (97 in a row)"]

    def test_a_partly_rejected_capability_reply_is_not_healthy(self) -> None:
        """The v7 parse bug: ``fresh_verified`` and green while v7 was gone."""
        now = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
        entry = self._entry(
            protocol_version=15,
            scorer={
                "status": "fresh_verified",
                "supported_bench_versions": [2, 3, 4, 5, 6],
                "observed_at": int(now.timestamp()),
                "software_version": "0.29.4",
                "source_revision": "a" * 40,
                "probe": {
                    "outcome": "served_degraded",
                    "observed_at": int(now.timestamp()),
                    "http_status": 200,
                    "reason": "calibration_unreadable",
                    "last_served_at": int(now.timestamp()),
                    "consecutive_failures": 1,
                },
            },
        )

        # Every other signal is still green: identity verified, stack healthy.
        assert entry.stack_health is not None
        assert entry.stack_health.dittobench_api.health == "healthy"
        assert entry.scorer_liveness == "degraded"
        assert entry.health == "warning"
        assert entry.health_reasons == ["scorer degraded: calibration_unreadable"]

    def test_a_serving_scorer_stays_healthy_and_carries_no_reasons(self) -> None:
        now = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
        entry = self._entry(
            protocol_version=15,
            scorer={
                "status": "fresh_verified",
                "supported_bench_versions": [2, 3],
                "observed_at": int(now.timestamp()),
                "software_version": "0.29.6",
                "source_revision": "a" * 40,
                "probe": {
                    "outcome": "served",
                    "observed_at": int(now.timestamp()),
                    "http_status": 200,
                    "last_served_at": int(now.timestamp()),
                },
            },
        )

        assert entry.scorer_liveness == "serving"
        assert entry.health_reasons == []
        assert entry.health == "healthy"

    def test_a_validator_below_protocol_15_reads_unreported_not_broken(self) -> None:
        """Forward compatibility: the fleet must not go red during the roll-out."""
        now = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
        entry = self._entry(
            protocol_version=14,
            scorer={
                "status": "fresh_verified",
                "supported_bench_versions": [2, 3],
                "observed_at": int(now.timestamp()),
                "software_version": "0.29.6",
                "source_revision": "a" * 40,
            },
        )

        assert entry.scorer_liveness == "unreported"
        assert entry.health_reasons == []
        assert entry.health != "critical"

    def test_a_v15_validator_that_reports_no_probe_is_still_called_out(self) -> None:
        """Silence from software that can speak is itself a finding."""
        now = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
        entry = self._entry(
            protocol_version=15,
            scorer={
                "status": "fresh_verified",
                "supported_bench_versions": [2, 3],
                "observed_at": int(now.timestamp()),
                "software_version": "0.29.6",
                "source_revision": "a" * 40,
            },
        )

        assert entry.scorer_liveness == "unreported"
        assert entry.health_reasons == ["scorer liveness not reported"]
        assert entry.health == "warning"

    @pytest.mark.parametrize(
        ("probe", "expected", "reason"),
        [
            pytest.param(
                {"outcome": "connect_error", "consecutive_failures": 3},
                "not_serving",
                "scorer not serving: connect_error (3 in a row)",
                id="refused",
            ),
            pytest.param(
                {"outcome": "timeout", "consecutive_failures": 1},
                "not_serving",
                "scorer not serving: timeout",
                id="timeout",
            ),
            pytest.param(
                {
                    "outcome": "http_error",
                    "http_status": 401,
                    "consecutive_failures": 1,
                },
                "not_serving",
                "scorer not serving: http 401",
                id="unauthorized",
            ),
            pytest.param(
                {
                    "outcome": "unreadable",
                    "http_status": 200,
                    "reason": "invalid_json",
                    "consecutive_failures": 1,
                },
                "not_serving",
                "scorer not serving: invalid_json",
                id="parse-error",
            ),
            pytest.param(
                {"outcome": "not_probed"},
                "unreported",
                "scorer was not probed",
                id="mock-mode",
            ),
        ],
    )
    def test_every_failure_mode_names_itself(
        self, probe: dict, expected: str, reason: str
    ) -> None:
        entry = self._entry(
            protocol_version=15,
            scorer={
                "status": "legacy_v2",
                "supported_bench_versions": [2],
                "probe": {"observed_at": 1_784_000_000, **probe},
            },
        )

        assert entry.scorer_liveness == expected
        assert entry.health_reasons == [reason]


def _v7_capable_row(
    now: datetime, *, hotkey: str, seen_at: datetime
) -> SimpleNamespace:
    """A heartbeat that clears every clause of the v7 capability gate."""
    revision = "a" * 40
    capabilities = {
        "screened_images": True,
        "require_screened_image": True,
        "source_build_fallback": False,
        "full_stack_managed": True,
        "stack_updater": True,
        "sandbox_egress_restricted": True,
        "ticket_inference": True,
        "signed_score_quorum": True,
        "executor_isolation": "privileged_dind",
        "scorer_benchmarks": {
            "status": "fresh_verified",
            "supported_bench_versions": [2, 3, 4, 5, 6, 7],
            "observed_at": int(now.timestamp()),
            "software_version": "1.3.0",
            "source_revision": revision,
            "v7_calibration": {
                "manifest_sha256": "c" * 64,
                "supported_routes": [
                    {
                        "provider": "Groq",
                        "profile_revision": "openrouter-route-test-v1",
                        "model": "openai/gpt-oss-20b",
                    }
                ],
            },
            "probe": {
                "outcome": "served",
                "observed_at": int(now.timestamp()),
                "http_status": 200,
                "last_served_at": int(now.timestamp()),
                "consecutive_failures": 0,
            },
        },
    }
    stack = {
        "mode": "source",
        "compose_schema": 1,
        "release_descriptor_digest": None,
        "components": {
            name: {
                "source_revision": revision if name == "dittobench_api" else "b" * 40,
                "version": "1.3.0" if name == "dittobench_api" else "1.2.0",
                "provenance": "committed_pin",
            }
            for name in (
                "ditto_subnet",
                "dittobench_api",
                "sandbox_docker",
                "model_relay",
                "pylon",
                "ollama",
            )
        },
    }
    return SimpleNamespace(
        validator_hotkey=hotkey,
        software_version="0.34.1",
        protocol_version=15,
        state="idle",
        active_agent_id=None,
        system_metrics={
            "collected_at": int(now.timestamp()),
            "cpu_percent": 10,
            "memory_percent": 20,
            "disk_percent": 30,
            "docker": {
                "status": "healthy",
                "running_containers": 2,
                "unhealthy_containers": 0,
            },
        },
        benchmark_progress=None,
        benchmark_capacity=None,
        capabilities=capabilities,
        stack=stack,
        stack_health=None,
        first_seen_at=now - timedelta(days=30),
        reported_at=seen_at,
        seen_at=seen_at,
    )


def _v8_only_capable_row(
    now: datetime, *, hotkey: str, seen_at: datetime
) -> SimpleNamespace:
    """The v0.44 heartbeat: v8 is verified without retired v7 metadata."""

    row = _v7_capable_row(now, hotkey=hotkey, seen_at=seen_at)
    row.protocol_version = 18
    scorer = row.capabilities["scorer_benchmarks"]
    scorer["supported_bench_versions"] = [8]
    scorer.pop("v7_calibration")
    return row


def _legacy_row(now: datetime, *, hotkey: str) -> SimpleNamespace:
    """The validator this gate exists for: ancient software, no capabilities.

    Protocol 6 predates the signed capability payload entirely, so it cannot
    advertise a benchmark and the platform leases it nothing. Its host metrics
    are deliberately spotless — that is exactly how it read as healthy.
    """
    return SimpleNamespace(
        validator_hotkey=hotkey,
        software_version="0.9.6",
        protocol_version=6,
        state="idle",
        active_agent_id=None,
        system_metrics={
            "collected_at": int(now.timestamp()),
            "cpu_percent": 0,
            "memory_percent": 10,
            "disk_percent": 5,
            "docker": {
                "status": "healthy",
                "running_containers": 1,
                "unhealthy_containers": 0,
            },
        },
        benchmark_progress=None,
        benchmark_capacity=None,
        capabilities=None,
        stack=None,
        stack_health=None,
        first_seen_at=now - timedelta(days=3),
        reported_at=now,
        seen_at=now,
    )


class TestActiveBenchCapabilityGate:
    """A validator that cannot serve the benchmark being scored earns nothing.

    Ticket issuance already gates every lease on ``heartbeat_supports_version``,
    so a stack that cannot serve the active benchmark is issued no work at all.
    Published as ``healthy`` and idle beside the fleet doing the work, it read as
    a spare validator rather than a spectator.
    """

    def _snapshot(self, rows: list[SimpleNamespace], *, version: int, now: datetime):
        return public_endpoint._validator_heartbeats_response(
            rows=rows,
            assignments=[],
            active_work=[],
            orphaned_leases=[],
            now=now,
            active_bench_version=version,
            slot_settings=SLOT_SETTINGS_DEFAULT,
        )

    def test_ancient_software_cannot_serve_the_active_benchmark(self) -> None:
        now = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
        snapshot = self._snapshot(
            [_legacy_row(now, hotkey=_VALIDATOR_C)], version=7, now=now
        )

        entry = snapshot.validators[0]
        assert entry.bench_serviceability == "software_obsolete"
        # Not a host problem, and not a warning: it cannot do its one job.
        assert entry.health == "critical"
        assert entry.health_reasons[0] == (
            "software too old for bench v7 (heartbeat protocol 6)"
        )
        # The window it is judged against travels with the verdict.
        assert snapshot.active_bench_version == 7

    def test_a_v7_capable_validator_passes_the_gate(self) -> None:
        now = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
        entry = self._snapshot(
            [_v7_capable_row(now, hotkey=_VALIDATOR_C, seen_at=now)],
            version=7,
            now=now,
        ).validators[0]

        assert entry.bench_serviceability == "serving"
        assert entry.health == "healthy"
        assert entry.health_reasons == []

    def test_a_v8_only_validator_passes_without_v7_calibration(self) -> None:
        now = datetime(2026, 8, 4, 20, 20, tzinfo=UTC)
        entry = self._snapshot(
            [_v8_only_capable_row(now, hotkey=_VALIDATOR_C, seen_at=now)],
            version=8,
            now=now,
        ).validators[0]

        assert entry.bench_serviceability == "serving"
        assert entry.health == "healthy"
        assert entry.health_reasons == []

    def test_a_quiet_validator_is_not_called_incapable(self) -> None:
        """Liveness and capability must not be conflated in either direction.

        A validator that stopped heartbeating an hour ago still advertises the
        active benchmark; calling it incapable would blame the wrong thing and
        would survive the reboot that fixes it.
        """
        now = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
        entry = self._snapshot(
            [
                _v7_capable_row(
                    now, hotkey=_VALIDATOR_C, seen_at=now - timedelta(hours=1)
                )
            ],
            version=7,
            now=now,
        ).validators[0]

        assert entry.availability == "offline"
        assert entry.bench_serviceability == "serving"
        assert entry.health_reasons == []

    def test_the_legacy_era_gates_nobody(self) -> None:
        """Mirror the leasing rule: below the legacy floor no capability is asked."""
        now = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
        snapshot = self._snapshot(
            [_legacy_row(now, hotkey=_VALIDATOR_C)],
            version=LEGACY_BENCH_VERSION,
            now=now,
        )

        entry = snapshot.validators[0]
        assert entry.bench_serviceability == "serving"
        assert entry.health == "healthy"

    def test_a_capable_stack_at_the_wrong_version_is_still_gated(self) -> None:
        """Support is per version: v7 support says nothing about v8."""
        now = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
        entry = self._snapshot(
            [_v7_capable_row(now, hotkey=_VALIDATOR_C, seen_at=now)],
            version=8,
            now=now,
        ).validators[0]

        assert entry.bench_serviceability == "scorer_unverified"
        assert entry.health_reasons[0] == "scorer not advertising bench v8"


class TestPublicFleet:
    def test_stale_boundaries_and_recovery_after_delayed_heartbeat(self) -> None:
        now = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)

        assert _fleet_classification(
            state="idle",
            seen_at=now - timedelta(minutes=5),
            now=now,
            metrics=None,
        )[:2] == (True, "available")
        assert _fleet_classification(
            state="running_benchmark",
            seen_at=now - timedelta(minutes=5, microseconds=1),
            now=now,
            metrics=None,
        )[:2] == (False, "stale")
        assert _fleet_classification(
            state="running_benchmark",
            seen_at=now - timedelta(minutes=15),
            now=now,
            metrics=None,
        )[:2] == (False, "stale")
        assert _fleet_classification(
            state="running_benchmark",
            seen_at=now - timedelta(minutes=15, microseconds=1),
            now=now,
            metrics=None,
        )[:2] == (False, "offline")
        assert _fleet_classification(
            state="running_benchmark", seen_at=now, now=now, metrics=None
        )[:2] == (True, "available")

    def test_stack_health_rolls_required_degraded_components_into_warning(
        self,
    ) -> None:
        def _component(
            health: ComponentHealthState, required: bool = True
        ) -> ValidatorComponentHealth:
            observed = None if health == "unknown" else 1_784_000_000
            ready = None if health in ("unknown", "unreachable") else True
            return ValidatorComponentHealth(
                health=health, required=required, observed_at=observed, ready=ready
            )

        def _stack(**overrides: ValidatorComponentHealth) -> ValidatorStackHealth:
            base = {
                name: _component("healthy")
                for name in ValidatorStackHealth.model_fields
            }
            base.update(overrides)
            return ValidatorStackHealth(**base)

        assert public_endpoint._stack_component_issues(None) == []
        assert public_endpoint._stack_component_issues(_stack()) == []
        current = ValidatorStackHealth(
            ditto_subnet=_component("healthy"),
            dittobench_api=_component("healthy"),
            sandbox_docker=_component("healthy"),
            pylon=_component("healthy"),
        )
        assert public_endpoint._stack_component_issues(current) == []
        # A reachable-but-degraded required scorer (its relay path is down) is
        # named with its exact state, not collapsed into a bare flag.
        assert public_endpoint._stack_component_issues(
            _stack(dittobench_api=_component("degraded"))
        ) == ["dittobench_api: degraded"]
        assert public_endpoint._stack_component_issues(
            _stack(model_relay=_component("unreachable"))
        ) == ["model_relay: unreachable"]
        # "unknown" is not-observed and must never raise a false warning.
        assert (
            public_endpoint._stack_component_issues(
                _stack(model_relay=_component("unknown"))
            )
            == []
        )
        # A non-required component in a bad state does not warn the fleet.
        assert (
            public_endpoint._stack_component_issues(
                _stack(pylon=_component("degraded", required=False))
            )
            == []
        )

    def test_health_reasons_name_every_cause_for_the_badge(self) -> None:
        def _component(
            health: ComponentHealthState, required: bool = True
        ) -> ValidatorComponentHealth:
            observed = None if health == "unknown" else 1_784_000_000
            ready = None if health in ("unknown", "unreachable") else True
            return ValidatorComponentHealth(
                health=health, required=required, observed_at=observed, ready=ready
            )

        def _stack(**overrides: ValidatorComponentHealth) -> ValidatorStackHealth:
            base = {
                name: _component("healthy")
                for name in ValidatorStackHealth.model_fields
            }
            base.update(overrides)
            return ValidatorStackHealth(**base)

        # A fully healthy validator carries no reasons.
        assert (
            public_endpoint._health_reasons(
                state="idle",
                metrics=PublicSystemMetrics(
                    cpu_percent=0,
                    memory_percent=10,
                    disk_percent=10,
                    docker_status="healthy",
                    running_containers=1,
                    unhealthy_containers=0,
                ),
                active_benchmark=None,
                stack_health=_stack(),
                scorer_reasons=[],
            )
            == []
        )
        # Every distinct cause is named; the stack cause carries the component.
        reasons = public_endpoint._health_reasons(
            state="idle",
            metrics=PublicSystemMetrics(
                cpu_percent=0,
                memory_percent=95,
                disk_percent=10,
                docker_status="healthy",
                running_containers=1,
                unhealthy_containers=0,
            ),
            active_benchmark=None,
            stack_health=_stack(dittobench_api=_component("degraded")),
            scorer_reasons=["scorer not serving: http 404"],
        )
        assert reasons == [
            "memory 95%",
            "dittobench_api: degraded",
            "scorer not serving: http 404",
        ]
        # No metrics reported explains an otherwise-unknown badge.
        assert public_endpoint._health_reasons(
            state="idle",
            metrics=None,
            active_benchmark=None,
            stack_health=None,
            scorer_reasons=[],
        ) == ["host metrics not reported"]

    async def test_validator_name_response_is_allowlisted_to_reporters(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_MINER_A,
                    software_version="1.2.3",
                    protocol_version=4,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                )
            )
        _install_db(app, session_maker)

        class Names:
            calls = 0

            def snapshot(self, hotkeys: list[str]) -> ValidatorNamesSnapshot:
                self.calls += 1
                assert hotkeys == [_MINER_A]
                return ValidatorNamesSnapshot(
                    status="fresh",
                    refreshed_at=now,
                    names={_MINER_A: "Rizzo", _MINER_B: "Not a reporter"},
                    stake_weights={_MINER_A: 123.5, _MINER_B: 456.0},
                )

        names = Names()
        app.state.validator_names = names
        response = await client.get("/api/v1/public/validator-names")

        assert response.status_code == 200
        assert (
            response.headers["Cache-Control"]
            == "public, max-age=30, stale-while-revalidate=120"
        )
        body = response.json()
        assert set(body) == {
            "generated_at",
            "source",
            "status",
            "refreshed_at",
            "validators",
        }
        assert body["source"] == "taostats"
        assert body["status"] == "fresh"
        assert body["validators"] == [
            {
                "validator_hotkey": _MINER_A,
                "display_name": "Rizzo",
                "stake_weight": 123.5,
            }
        ]
        assert names.calls == 1

    async def test_core_fleet_endpoint_never_reads_external_name_cache(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)

        class ExplodingNames:
            def snapshot(self, hotkeys: list[str]) -> ValidatorNamesSnapshot:
                raise AssertionError(f"unexpected name lookup for {hotkeys}")

        app.state.validator_names = ExplodingNames()
        response = await client.get("/api/v1/public/validators")

        assert response.status_code == 200
        assert response.json()["validators"] == []


class TestPublicActivity:
    async def test_agent_summary_is_a_targeted_glance_level_projection(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            name="hot-path-agent",
            status=AgentStatus.UPLOADED,
        )
        _install_db(app, session_maker)
        # The deep link must never fall back to the global projection that loads
        # and derives the entire activity population before applying its search.
        monkeypatch.setattr(
            public_endpoint,
            "list_public_activity",
            AsyncMock(side_effect=AssertionError("global activity query used")),
        )

        response = await client.get(f"/api/v1/public/agent/{agent_id}/summary")

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "generated_at": body["generated_at"],
            "agent_id": agent_id,
            "miner_hotkey": _MINER_A,
            "name": "hot-path-agent",
            "version": None,
            "status": "waiting_screening",
            "submitted_at": body["submitted_at"],
            "last_scored_at": None,
            "score_count": 0,
            "score_composite": None,
            "quorum": 3,
            "screening_reason": None,
            "duplicate_of": None,
            "duplicate_name": None,
            "duplicate_version": None,
            "review_reason": None,
            "review_event": None,
            "review_event_at": None,
            "review_original_reason": None,
            "review_opened_at": None,
            "preserved_composite": None,
            "active_benchmarks": [],
        }
        assert "artifact_release" not in body
        assert "screening_attempts" not in body
        assert "validation_attempts" not in body
        assert "provisional_scores" not in body

    async def test_agent_summary_reports_the_canonical_median_not_the_mean(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _activate_era(session_maker)
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.1, 0.2, 0.9],
        )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/summary")

        assert response.status_code == 200
        body = response.json()
        assert body["score_count"] == body["quorum"] == 3
        assert body["score_composite"] == pytest.approx(0.2)

    async def test_agent_summary_returns_not_found_without_global_activity(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_db(app, session_maker)
        monkeypatch.setattr(
            public_endpoint,
            "list_public_activity",
            AsyncMock(side_effect=AssertionError("global activity query used")),
        )

        response = await client.get(f"/api/v1/public/agent/{uuid4()}/summary")

        assert response.status_code == 404

    async def test_operations_lists_desired_rollout_members_as_queue_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A settled agent remains visible while the next benchmark collects."""
        await _activate_era(session_maker)
        member_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.SCORED,
            name="rollout-member",
            created_at=datetime(2026, 7, 31, 20, 0, tzinfo=UTC),
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        rollout_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_ERA,
                    desired_version=_NEXT_ERA,
                    status="collecting",
                    cohort_size=5,
                    rescore_cohort_target=5,
                    priority_cohort_target=5,
                    created_at=datetime(2026, 7, 31, 21, 0, tzinfo=UTC),
                )
            )
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout_id,
                    agent_id=UUID(member_id),
                    position=1,
                    frozen_miner_hotkey=_MINER_A,
                    frozen_composite=0.9,
                )
            )
            session.add(_dataset_pin(UUID(member_id), bench_version=_NEXT_ERA))
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/operations")

        assert response.status_code == 200
        body = response.json()
        assert body["active_bench_version"] == _ERA
        assert body["desired_bench_version"] == _NEXT_ERA
        assert body["rollout_queue"] == [
            {
                "agent_id": member_id,
                "miner_hotkey": _MINER_A,
                "name": "rollout-member",
                "version": None,
                "submitted_at": "2026-07-31T20:00:00Z",
                "bench_version": _NEXT_ERA,
                "position": 1,
                "status": "waiting_validator",
                "score_count": 0,
                "quorum": 3,
                "retry_state": "queued",
                "retry_after": None,
                "active_benchmarks": [],
            }
        ]

    async def test_lists_all_stages_newest_first_without_sensitive_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        older_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.UPLOADED,
            name="memory-v1",
            created_at=datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC),
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.ATH_PENDING_REVIEW,
            name="memory-v2",
            created_at=datetime(2026, 7, 13, 11, 0, 0, tzinfo=UTC),
            duplicate_of=UUID(older_id),
            review_reason=(
                f"content near-duplicate of agent {older_id}: "
                "composite delta 0.0010, jaccard 0.950"
            ),
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.BANNED,
            name="memory-v3",
            created_at=datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC),
            screening_reason="Docker image build failed",
        )
        _install_db(app, session_maker)

        resp = await client.get("/api/v1/public/activity")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "public, max-age=10"
        body = resp.json()
        assert body["count"] == 3
        assert body["total"] == 3
        assert body["page"] == 1
        assert body["page_size"] == 50
        assert body["total_pages"] == 1
        assert [entry["name"] for entry in body["entries"]] == [
            "memory-v3",
            "memory-v2",
            "memory-v1",
        ]
        assert [entry["status"] for entry in body["entries"]] == [
            "rejected",
            "under_review",
            "waiting_screening",
        ]
        assert body["entries"][2]["agent_id"] == older_id
        assert body["entries"][0]["screening_reason"] == "Docker image build failed"
        assert body["entries"][1]["duplicate_of"] == older_id
        assert body["entries"][1]["duplicate_name"] == "memory-v1"
        assert body["entries"][1]["duplicate_version"] is None
        assert "jaccard 0.950" in body["entries"][1]["review_reason"]
        assert set(body["entries"][0]) == {
            "agent_id",
            "miner_hotkey",
            "name",
            "version",
            "status",
            "artifact_release",
            "submitted_at",
            "last_scored_at",
            "screening_reason",
            "duplicate_of",
            "duplicate_name",
            "duplicate_version",
            "review_reason",
            "review_event",
            "review_event_at",
            "review_original_reason",
            "review_opened_at",
            "preserved_composite",
            "score_count",
            "provisional_composite",
            "validator_queue_rank",
            "validator_queue_gate",
            "validator_queue_gate_detail",
            "previous_generation",
            "quorum",
            "retry_state",
            "retry_after",
            "screening_policy_version",
            "required_screening_policy_version",
            "screening_attempt_id",
            "screening_build_only",
            "screening_started_at",
            "screening_deadline",
            "active_benchmarks",
        }
        serialized = resp.text
        for private_field in (
            "sha256",
            "download_url",
            "payment",
            "SECRET_FROM_BUILD",
        ):
            assert private_field not in serialized

    async def test_ath_review_filter_is_public_safe_and_includes_hold_snapshot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        held_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.4, 0.8, 0.9],
                status=AgentStatus.ATH_PENDING_REVIEW,
            )
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.QUARANTINED,
            name="screening-review",
        )
        opened_at = datetime(2026, 7, 16, 15, 30, tzinfo=UTC)
        async with session_maker() as session, session.begin():
            held = await session.get(Agent, held_id)
            assert held is not None
            held.name = "memory-harness"
            held.version = 4
            held.review_reason = "Submission requires ATH similarity review"
            session.add(
                AthReview(
                    review_id=uuid4(),
                    agent_id=held_id,
                    status="pending",
                    opened_at=opened_at,
                    original_duplicate_of=None,
                    original_reason=held.review_reason,
                    original_policy_version=8,
                    original_evidence={
                        "sha256": held.sha256,
                        "challenge_value": "private-challenge",
                        "answer_key": "private-answer-key",
                        "source_path": "/private/source.rs",
                    },
                    algorithm_provenance={
                        "opened_by": "private-operator",
                        "credential": "private-credential",
                    },
                )
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get(
            "/api/v1/public/activity?review=ath&status=under_review&limit=200"
        )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=10"
        body = response.json()
        assert body["total"] == body["count"] == 1
        entry = body["entries"][0]
        assert entry["agent_id"] == str(held_id)
        assert entry["name"] == "memory-harness"
        assert entry["version"] == 4
        assert entry["miner_hotkey"] == _MINER_A
        assert entry["status"] == "under_review"
        assert datetime.fromisoformat(entry["review_opened_at"]) == opened_at
        assert entry["review_reason"] == "Submission requires ATH similarity review"
        assert entry["score_count"] == 3
        assert entry["provisional_composite"] == pytest.approx(0.7)
        assert entry["preserved_composite"] == pytest.approx(0.8)
        serialized = response.text.lower()
        for private_value in (
            "sha256",
            "private-challenge",
            "private-answer-key",
            "private/source.rs",
            "private-operator",
            "private-credential",
            "opened_by",
        ):
            assert private_value not in serialized

    async def test_activity_projects_latest_reopen_reason_not_original_copy_reason(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        held_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.61, 0.62, 0.63],
                status=AgentStatus.ATH_PENDING_REVIEW,
            )
        )
        review_id = uuid4()
        opened_at = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)
        cleared_at = datetime(2026, 7, 31, 20, 5, tzinfo=UTC)
        reopened_at = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
        original_reason = "Possible source similarity requires operator review."
        reopen_reason = "Manual benchmark-integrity review of the scored artifact."
        async with session_maker() as session, session.begin():
            held = await session.get(Agent, held_id)
            assert held is not None
            # Lifecycle guards intentionally retain this historical value.
            held.review_reason = original_reason
            session.add(
                AthReview(
                    review_id=review_id,
                    agent_id=held_id,
                    status="pending",
                    opened_at=opened_at,
                    reopened_at=reopened_at,
                    original_duplicate_of=None,
                    original_reason=original_reason,
                    original_policy_version=9,
                    original_evidence={"sha256": held.sha256},
                    algorithm_provenance={"review_kind": "copy"},
                )
            )
            session.add_all(
                [
                    AthReviewAction(
                        action_id=uuid4(),
                        review_id=review_id,
                        action="clear",
                        reason="Same-owner lineage verified.",
                        actor="operator@example.com",
                        evidence={"previous_status": "scored"},
                        created_at=cleared_at,
                    ),
                    AthReviewAction(
                        action_id=uuid4(),
                        review_id=review_id,
                        action="reopen",
                        reason=reopen_reason,
                        actor="operator@example.com",
                        evidence={
                            "previous_status": "scored",
                            "sha256": held.sha256,
                            "score_count": 3,
                        },
                        created_at=reopened_at,
                    ),
                ]
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get(
            "/api/v1/public/activity?review=ath&status=under_review&limit=200"
        )

        assert response.status_code == 200
        entry = next(
            row for row in response.json()["entries"] if row["agent_id"] == str(held_id)
        )
        assert entry["review_event"] == "reopened"
        assert datetime.fromisoformat(entry["review_event_at"]) == reopened_at
        assert datetime.fromisoformat(entry["review_opened_at"]) == reopened_at
        assert entry["review_reason"] == reopen_reason
        assert entry["review_original_reason"] == original_reason
        assert "Same-owner lineage verified" not in response.text
        assert "operator@example.com" not in response.text

    async def test_activity_projects_resolution_reason_for_resolved_review(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        resolved_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.41, 0.42, 0.43],
                status=AgentStatus.SCORED,
            )
        )
        review_id = uuid4()
        opened_at = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)
        resolved_at = datetime(2026, 7, 31, 20, 5, tzinfo=UTC)
        original_reason = "Possible source similarity requires operator review."
        resolution_reason = "Same-owner lineage verified; score eligibility restored."
        async with session_maker() as session, session.begin():
            resolved = await session.get(Agent, resolved_id)
            assert resolved is not None
            resolved.review_reason = original_reason
            session.add(
                AthReview(
                    review_id=review_id,
                    agent_id=resolved_id,
                    status="resolved",
                    opened_at=opened_at,
                    resolved_at=resolved_at,
                    resolved_by="operator@example.com",
                    resolution="clear",
                    resolution_reason=resolution_reason,
                    original_duplicate_of=None,
                    original_reason=original_reason,
                    original_policy_version=9,
                    original_evidence={"sha256": resolved.sha256},
                    algorithm_provenance={"review_kind": "copy"},
                )
            )
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=review_id,
                    action="clear",
                    reason=resolution_reason,
                    actor="operator@example.com",
                    evidence={"previous_status": "scored"},
                    created_at=resolved_at,
                )
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get(
            f"/api/v1/public/activity?q={resolved_id}&limit=200"
        )

        assert response.status_code == 200
        entry = response.json()["entries"][0]
        assert entry["review_event"] == "cleared"
        assert datetime.fromisoformat(entry["review_event_at"]) == resolved_at
        assert entry["review_reason"] == resolution_reason
        assert entry["review_original_reason"] == original_reason
        assert "operator@example.com" not in response.text

    async def test_previous_generation_rows_rank_behind_the_current_era(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A stranded backlog must not appear to hold the head of the queue.

        Pre-rollout submissions are served only by the carryover and
        source-backfill lanes, which issue into an empty current-era queue and
        nothing else. Ranking them by arrival alone told miners the opposite of
        the order the fleet actually works in -- the exact report this fixes.
        """
        rollout_started = datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
        stranded_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.EVALUATING,
            name="stranded-prev-gen",
            created_at=rollout_started - timedelta(days=3),
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        fresh_id = await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.EVALUATING,
            name="fresh-current-era",
            created_at=rollout_started + timedelta(days=1),
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        rollout_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                # Live production shape: authority taken, qualification still
                # settling, so the rollout row is "collecting" not "activated".
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_ERA - 1,
                    desired_version=_ERA,
                    status="collecting",
                    cohort_size=5,
                    created_at=rollout_started,
                )
            )
        await _take_authority(session_maker, rollout_id=rollout_id, at=rollout_started)
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/activity")
        by_id = {entry["agent_id"]: entry for entry in response.json()["entries"]}

        # Older by arrival, last by priority.
        assert by_id[fresh_id]["validator_queue_rank"] == 1
        assert by_id[stranded_id]["validator_queue_rank"] == 2
        assert by_id[stranded_id]["status"] == "waiting_validator"
        # The rank number alone cannot say *why* the row is last, so the era is
        # reported too. Both rows are "waiting_validator" and both carry an
        # integer rank; only this distinguishes a stranded row from a queued one.
        assert by_id[stranded_id]["previous_generation"] is True
        assert by_id[fresh_id]["previous_generation"] is False
        # ``previous_generation`` is the era-specific case of the general gate,
        # read off the same shared preview so the two cannot disagree.
        assert by_id[stranded_id]["validator_queue_gate"] == "previous_generation"
        assert by_id[fresh_id]["validator_queue_gate"] is None

        # The operations board renders the pipeline the miners actually look
        # at, and it never received #448's ranking fix: it called the
        # projection without the previous-generation set at all, so a stranded
        # backlog kept holding rank 1 there -- badged "Up next" -- after
        # /activity was corrected. Both endpoints now go through one preview.
        operations = (await client.get("/api/v1/public/operations")).json()
        ops_by_id = {
            entry["agent_id"]: entry for entry in operations["activity"]["entries"]
        }
        assert ops_by_id[fresh_id]["validator_queue_rank"] == 1
        assert ops_by_id[stranded_id]["validator_queue_rank"] == 2
        assert ops_by_id[stranded_id]["validator_queue_gate"] == "previous_generation"
        assert ops_by_id[stranded_id]["previous_generation"] is True

    async def test_flags_a_previous_generation_row_that_holds_rank_one(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Being first in a stalled line is not the same as being next.

        Sorting stranded rows last fixes the common case, but it cannot fix this
        one: once the current era has nothing waiting, the top of the queue is a
        previous-generation row and its rank is 1. The dashboard badges rank 1 as
        "Up next", which a miner reads as "about to be scored" -- while the lanes
        that serve this row issue only into an empty current-era queue and may
        stay shut indefinitely. The flag is what lets the client tell the truth,
        so it must survive precisely the arrangement that makes rank misleading.
        """
        rollout_started = datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
        stranded_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.EVALUATING,
            name="stranded-alone",
            created_at=rollout_started - timedelta(days=3),
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        rollout_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_ERA - 1,
                    desired_version=_ERA,
                    status="collecting",
                    cohort_size=5,
                    created_at=rollout_started,
                )
            )
        await _take_authority(session_maker, rollout_id=rollout_id, at=rollout_started)
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/activity")
        by_id = {entry["agent_id"]: entry for entry in response.json()["entries"]}

        assert by_id[stranded_id]["validator_queue_rank"] == 1
        assert by_id[stranded_id]["previous_generation"] is True
        # The gate is what the badge must key on: rank 1 in a stalled lane is
        # exactly the arrangement that made "Up next" a lie.
        assert by_id[stranded_id]["validator_queue_gate"] == "previous_generation"

    async def test_exposes_queue_priority_with_provisional_composites(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_top_ten_floor(session_maker, tenth_place=0.60)
        zero_id = await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.EVALUATING,
            name="zero",
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        one_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.5],
            status=AgentStatus.EVALUATING,
        )
        one_high_id = await _seed_k3(
            session_maker,
            miner=_VALIDATOR_C,
            composites=[0.95],
            status=AgentStatus.EVALUATING,
        )
        low_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.2, 0.3],
            status=AgentStatus.EVALUATING,
        )
        high_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.8, 0.9],
            status=AgentStatus.EVALUATING,
        )
        async with session_maker() as session, session.begin():
            for agent_id in (one_id, one_high_id, low_id, high_id):
                agent = await session.get(Agent, UUID(agent_id))
                assert agent is not None
                agent.screening_policy_version = SCREENING_POLICY_VERSION
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/activity")
        by_id = {entry["agent_id"]: entry for entry in response.json()["entries"]}

        assert by_id[one_high_id]["validator_queue_rank"] == 1
        assert by_id[high_id]["validator_queue_rank"] == 2
        assert by_id[zero_id]["validator_queue_rank"] == 3
        assert by_id[low_id]["validator_queue_rank"] == 4
        # ``one`` and ``high`` are the same miner's submissions, and ``high``
        # has already started progressing, so the allocator pins that owner's
        # single slot to it. ``one`` cannot be leased until ``high`` settles --
        # previously the preview ranked it fourth as though nothing were in its
        # way, which is the "why isn't mine moving" case with no explanation.
        assert by_id[one_id]["validator_queue_rank"] == 5
        assert by_id[one_id]["validator_queue_gate"] == "owner_serialized"
        assert by_id[low_id]["status"] == "below_score_floor"
        # A below-floor row is still leasable, just last.
        assert by_id[low_id]["validator_queue_gate"] is None
        assert by_id[zero_id]["provisional_composite"] is None
        assert by_id[one_id]["provisional_composite"] == pytest.approx(0.5)
        assert by_id[one_high_id]["provisional_composite"] == pytest.approx(0.95)
        assert by_id[high_id]["provisional_composite"] == pytest.approx(0.85)
        assert by_id[low_id]["provisional_composite"] == pytest.approx(0.25)

    async def test_contender_lane_gives_one_slot_per_coldkey_not_per_hotkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The preview must group contenders the way the allocator does.

        ``issue_ticket``'s contender lane partitions on the payment-time coldkey
        (``emission_owner_key``), so one coldkey funding two hotkeys occupies a
        single contender slot. Grouping the preview by hotkey handed that owner
        two slots and pushed every miner below it one rank too deep.
        """
        shared_coldkey = "5SharedColdkeyFundingTwoHotkeys"
        best_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.90],
            status=AgentStatus.EVALUATING,
        )
        sibling_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.89],
            status=AgentStatus.EVALUATING,
        )
        unscored_id = await _seed_agent(
            session_maker,
            miner=_VALIDATOR_C,
            status=AgentStatus.EVALUATING,
            name="unscored",
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        await _seed_payment(
            session_maker,
            agent_id=best_id,
            miner_hotkey=_MINER_A,
            miner_coldkey=shared_coldkey,
            index=1,
        )
        await _seed_payment(
            session_maker,
            agent_id=sibling_id,
            miner_hotkey=_MINER_B,
            miner_coldkey=shared_coldkey,
            index=2,
        )
        await _seed_payment(
            session_maker,
            agent_id=unscored_id,
            miner_hotkey=_VALIDATOR_C,
            miner_coldkey="5IndependentColdkey",
            index=3,
        )
        async with session_maker() as session, session.begin():
            for agent_id in (best_id, sibling_id):
                agent = await session.get(Agent, UUID(agent_id))
                assert agent is not None
                agent.screening_policy_version = SCREENING_POLICY_VERSION
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/activity")
        by_id = {entry["agent_id"]: entry for entry in response.json()["entries"]}

        # Only the owner's best submission takes a contender slot. Its sibling
        # drops to the ordinary queue, where the untouched submission's coverage
        # priority (zero scores) puts it ahead.
        assert by_id[best_id]["validator_queue_rank"] == 1
        assert by_id[unscored_id]["validator_queue_rank"] == 2
        assert by_id[sibling_id]["validator_queue_rank"] == 3

    async def test_filters_complete_dataset_before_paginating_with_counts(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        for index in range(12):
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.UPLOADED,
                name=f"queued-{index}",
                created_at=datetime(2026, 7, 13, 10, index, tzinfo=UTC),
            )
        await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.BANNED,
            name="rejected-late",
            created_at=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        )
        _install_db(app, session_maker)

        body = (
            await client.get("/api/v1/public/activity?status=rejected&page=1&limit=10")
        ).json()

        assert body["total"] == 1
        assert body["count"] == 1
        assert body["entries"][0]["name"] == "rejected-late"
        assert body["status_counts"]["waiting_screening"] == 12
        assert body["status_counts"]["rejected"] == 1

    async def test_combines_states_and_composes_with_search(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.UPLOADED,
            name="alpha queued",
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.SCREENING,
            name="alpha screening",
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.BANNED,
            name="alpha rejected",
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.UPLOADED,
            name="beta queued",
        )
        _install_db(app, session_maker)

        response = await client.get(
            "/api/v1/public/activity",
            params=[
                ("status", "waiting_screening"),
                ("status", "screening"),
                ("q", "alpha"),
            ],
        )

        assert response.status_code == 200
        body = response.json()
        assert {entry["name"] for entry in body["entries"]} == {
            "alpha queued",
            "alpha screening",
        }
        assert body["total"] == 2
        assert body["status_counts"] == {
            "waiting_screening": 1,
            "screening": 1,
            "rejected": 1,
        }

    async def test_rejects_unknown_public_status_filter(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/activity?status=obsolete")

        assert response.status_code == 422
        assert "unknown public activity status: obsolete" in response.text

    async def test_filters_downloadable_agents_and_composes_with_search(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        downloadable_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.61, 0.64, 0.67],
            status=AgentStatus.LIVE,
        )
        await _crown(
            session_maker,
            agent_id=downloadable_id,
            first_crowned_at=now - timedelta(hours=49),
        )
        await _seed_agent(
            session_maker,
            miner=_MINER_B,
            status=AgentStatus.LIVE,
            name="alpha private",
        )
        _install_db(app, session_maker)

        response = await client.get(
            "/api/v1/public/activity",
            params={"downloadable": "true", "q": downloadable_id},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["downloadable_count"] == 1
        assert body["total"] == 1
        assert [entry["agent_id"] for entry in body["entries"]] == [downloadable_id]
        assert body["entries"][0]["artifact_release"]["download_available"] is True

        no_matches = (
            await client.get(
                "/api/v1/public/activity",
                params={"downloadable": "true", "status": "rejected"},
            )
        ).json()
        assert no_matches["downloadable_count"] == 1
        assert no_matches["total"] == 0

    async def test_exposes_latest_platform_score_time_for_finalized_agents(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        recorded_at = datetime(2026, 7, 14, 9, 30, 0, tzinfo=UTC)
        agent_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.61, 0.64, 0.67],
                status=AgentStatus.LIVE,
                # Validator provenance may be stale or inaccurate and must not drive
                # the public dashboard's relative score age.
                base_time=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        async with session_maker() as session, session.begin():
            scores = (
                (await session.execute(select(Score).where(Score.agent_id == agent_id)))
                .scalars()
                .all()
            )
            for index, score in enumerate(scores):
                score.created_at = recorded_at - timedelta(minutes=index)
                score.updated_at = recorded_at - timedelta(minutes=index)

        await _activate_era(session_maker)
        _install_db(app, session_maker)

        entry = (await client.get("/api/v1/public/activity")).json()["entries"][0]

        assert entry["status"] == "live"
        assert entry["score_count"] == 3
        assert datetime.fromisoformat(entry["last_scored_at"]) == recorded_at

    async def test_active_rescreen_projects_yellow_and_exposes_version_history(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.SCREENING,
                screening_reason="Container failed the health check",
                screening_policy_version=SCREENING_POLICY_VERSION - 1,
            )
        )
        now = datetime.now(UTC)
        old_attempt_id = uuid4()
        active_attempt_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add_all(
                [
                    ScreeningAttempt(
                        attempt_id=old_attempt_id,
                        agent_id=agent_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION - 1,
                        status="rejected",
                        started_at=now - timedelta(hours=1),
                        deadline=now - timedelta(minutes=40),
                        finished_at=now - timedelta(minutes=45),
                        public_reason="Container failed the health check",
                    ),
                    ScreeningAttempt(
                        attempt_id=active_attempt_id,
                        agent_id=agent_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        status="running",
                        started_at=now,
                        deadline=now + timedelta(minutes=30),
                    ),
                ]
            )
        _install_db(app, session_maker)

        activity = (await client.get("/api/v1/public/activity")).json()["entries"][0]
        assert activity["status"] == "screening"
        assert activity["screening_reason"] is None
        assert activity["screening_policy_version"] == SCREENING_POLICY_VERSION - 1
        assert activity["required_screening_policy_version"] == SCREENING_POLICY_VERSION
        assert activity["screening_attempt_id"] == str(active_attempt_id)
        assert activity["screening_build_only"] is False

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "screening"
        assert [
            attempt["policy_version"] for attempt in body["screening_attempts"]
        ] == [
            SCREENING_POLICY_VERSION,
            SCREENING_POLICY_VERSION - 1,
        ]
        assert [attempt["status"] for attempt in body["screening_attempts"]] == [
            "running",
            "rejected",
        ]

    async def test_each_score_carries_its_own_bench_versions_dataset_digest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A newer score must not be published with the older era's digest.

        Dataset provenance is per bench version, but the agent row carries only
        the version it was first pinned at. Pairing every score with that column
        advertised the older digest next to a verification_command naming the
        newer era, so a verifier would render the newer era and get a mismatch
        on a perfectly good score.
        """
        agent_id = uuid4()
        era_sha, next_sha = "a1" * 32, "b2" * 32
        async with session_maker() as session, session.begin():
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=_MINER_A,
                    name="agent",
                    sha256="ab" * 32,
                    size_bytes=524288,
                    status=AgentStatus.SCORED,
                    dataset_seed=42,
                    dataset_sha256=era_sha,
                    dataset_run_size="full",
                    created_at=datetime.now(UTC),
                )
            )
            await session.flush()
            # Only the newer era is pinned; the older one falls back to the
            # agent column, which is exactly the mixed state production is in.
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_NEXT_ERA,
                    seed=42,
                    sha256=next_sha,
                    run_size="full",
                )
            )
            for bench_version, hotkey in ((_ERA, _VALIDATOR_C), (_NEXT_ERA, _MINER_B)):
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=hotkey,
                    bench_version=bench_version,
                    run_id=f"run_{bench_version}",
                    seed=42,
                    composite=0.9,
                    tool_mean=0.9,
                    memory_mean=0.9,
                    median_ms=500,
                    n=20,
                    generated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
                    signature="ab" * 64,
                    details={"bench_version": bench_version},
                )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        by_version = {
            score["bench_version"]: score["dataset_sha256"]
            for score in response.json()["provisional_scores"]
        }
        assert by_version == {_ERA: era_sha, _NEXT_ERA: next_sha}

    async def test_stale_rejection_projects_as_waiting_for_rescreen(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(
            session_maker,
            miner=_MINER_A,
            status=AgentStatus.REJECTED,
            screening_reason="Container failed the health check",
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        _install_db(app, session_maker)

        entry = (await client.get("/api/v1/public/activity")).json()["entries"][0]
        assert entry["status"] == "waiting_screening"
        assert entry["screening_reason"] is None

    async def test_quarantined_attempt_history_is_publicly_serializable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.QUARANTINED,
                screening_reason="Submission held for anti-cheat review",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        attempt_id = uuid4()
        private_finding = SourceReviewFinding(
            artifact_sha256="cd" * 32,
            prompt_revision="private-pending-review-v1",
            risk_level="high",
            confidence=0.99,
            categories=["answer_mutation"],
            evidence=[
                SourceReviewEvidenceItem(
                    path="src/private_innovation.rs",
                    line=41,
                    category="answer_mutation",
                )
            ],
            summary="Pending finding must remain private until a terminal rejection.",
        )
        async with session_maker() as session, session.begin():
            session.add_all(
                [
                    ScreeningAttempt(
                        attempt_id=attempt_id,
                        agent_id=agent_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        status="quarantined",
                        started_at=now - timedelta(minutes=2),
                        deadline=now + timedelta(minutes=28),
                        finished_at=now,
                        public_reason="Submission held for anti-cheat review",
                    ),
                    ScreeningQuarantine(
                        quarantine_id=uuid4(),
                        agent_id=agent_id,
                        attempt_id=attempt_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        manifest_digest="ab" * 32,
                        finding_digest=private_finding.canonical_digest(),
                        reason_code="suspicious-source",
                        evidence=[
                            {
                                "module_id": "agentic-source-review",
                                "code": "pending-private-review",
                                "summary": "Pending evidence remains private.",
                                "digest": None,
                            }
                        ],
                        finding=private_finding.model_dump(mode="json"),
                        status="active",
                    ),
                ]
            )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        attempt = response.json()["screening_attempts"][0]
        assert attempt["status"] == "quarantined"
        assert attempt["quarantine_resolution"] is None
        assert attempt["quarantine_resolved_at"] is None
        assert attempt["quarantine_resolution_reason"] is None
        assert attempt["review_evidence"] == []
        assert attempt["review_finding"] is None

    async def test_released_quarantine_resolution_is_public_in_attempt_history(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_reason="Manual review found no prohibited behavior",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        attempt_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add_all(
                [
                    ScreeningAttempt(
                        attempt_id=attempt_id,
                        agent_id=agent_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        status="quarantined",
                        started_at=now - timedelta(minutes=12),
                        deadline=now + timedelta(minutes=18),
                        finished_at=now - timedelta(minutes=10),
                        public_reason="Submission held for anti-cheat review",
                    ),
                    ScreeningQuarantine(
                        quarantine_id=uuid4(),
                        agent_id=agent_id,
                        attempt_id=attempt_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        manifest_digest="ab" * 32,
                        reason_code="suspicious-source",
                        status="resolved",
                        resolved_at=now,
                        resolved_by="admin@example.com",
                        resolution="release",
                        resolution_reason=(
                            "Manual review found no prohibited behavior"
                        ),
                    ),
                ]
            )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        attempt = response.json()["screening_attempts"][0]
        assert attempt["status"] == "quarantined"
        assert attempt["quarantine_resolution"] == "release"
        assert datetime.fromisoformat(attempt["quarantine_resolved_at"]) == now
        assert attempt["quarantine_resolution_reason"] == (
            "Manual review found no prohibited behavior"
        )
        assert "resolved_by" not in attempt
        assert attempt["review_evidence"] == []
        assert attempt["review_finding"] is None

    async def test_rejected_quarantine_publishes_only_digest_verified_review(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.REJECTED,
                screening_reason="Submission violated the anti-cheat policy",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        attempt_id = uuid4()
        finding = SourceReviewFinding(
            artifact_sha256="cd" * 32,
            prompt_revision="public-safe-review-v1",
            risk_level="high",
            confidence=0.99,
            categories=["answer_mutation"],
            evidence=[
                SourceReviewEvidenceItem(
                    path="src/response.rs",
                    line=73,
                    category="answer_mutation",
                )
            ],
            summary=(
                "A reachable policy-controlled branch replaces the authoritative "
                "model answer before the response is returned."
            ),
        )
        async with session_maker() as session, session.begin():
            session.add_all(
                [
                    ScreeningAttempt(
                        attempt_id=attempt_id,
                        agent_id=agent_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        status="quarantined",
                        started_at=now - timedelta(minutes=12),
                        deadline=now + timedelta(minutes=18),
                        finished_at=now - timedelta(minutes=10),
                        public_reason="Submission held for anti-cheat review",
                    ),
                    ScreeningQuarantine(
                        quarantine_id=uuid4(),
                        agent_id=agent_id,
                        attempt_id=attempt_id,
                        screener_hotkey=_MINER_B,
                        policy_version=SCREENING_POLICY_VERSION,
                        manifest_digest="ab" * 32,
                        finding_digest=finding.canonical_digest(),
                        reason_code="suspicious-source",
                        evidence=[
                            {
                                "module_id": "agentic-source-review",
                                "code": "answer-authority-violation",
                                "summary": (
                                    "The served response path replaces a model-"
                                    "authored answer with policy-controlled output."
                                ),
                                "digest": "ef" * 32,
                            }
                        ],
                        finding=finding.model_dump(mode="json"),
                        status="resolved",
                        resolved_at=now,
                        resolved_by="automation:screening-policy-v9",
                        resolution="reject",
                        resolution_reason="Verified prohibited answer replacement",
                    ),
                ]
            )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        attempt = response.json()["screening_attempts"][0]
        assert attempt["review_evidence"] == [
            {
                "module": "agentic-source-review",
                "code": "answer-authority-violation",
                "summary": (
                    "The served response path replaces a model-authored answer with "
                    "policy-controlled output."
                ),
            }
        ]
        assert attempt["review_finding"] == {
            "reviewer_revision": "public-safe-review-v1",
            "risk_level": "high",
            "confidence": 0.99,
            "categories": ["answer_mutation"],
            "locations": [
                {
                    "path": "src/response.rs",
                    "line": 73,
                    "category": "answer_mutation",
                }
            ],
            "summary": finding.summary,
        }
        assert "artifact_sha256" not in attempt["review_finding"]
        assert "digest" not in attempt["review_evidence"][0]

    async def test_evaluation_projects_live_work_from_validator_heartbeat(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        waiting = (await client.get("/api/v1/public/activity")).json()["entries"][0]
        assert waiting["status"] == "waiting_validator"

        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_MINER_B,
                    software_version="1.2.3",
                    protocol_version=2,
                    code_digest="ab" * 32,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_B,
                    bench_version=_ERA,
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(seconds=1),
                    deadline=now + timedelta(minutes=30),
                )
            )

        evaluating = (await client.get("/api/v1/public/activity")).json()["entries"][0]
        assert evaluating["status"] == "evaluating"

    async def test_two_scores_below_top_five_bound_are_queued_for_completion(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_top_five_floor(session_maker, fifth_place=0.80)
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        async with session_maker() as session, session.begin():
            for index, (validator, composite) in enumerate(
                ((_VALIDATOR_C, 0.10), (_MINER_B, 0.20))
            ):
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=validator,
                    bench_version=_ERA,
                    run_id=f"below-floor-{index}",
                    seed=42,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=500,
                    n=114,
                    generated_at=datetime.now(UTC),
                    signature="ab" * 64,
                    details={
                        "per_case": [
                            {
                                "kind": "memory",
                                "category": "temporal_reasoning",
                                "score": composite,
                                "correct": False,
                                "latency_ms": 500,
                                "notes": ["no deterministic value match"],
                                "expected": "private answer key",
                                "called": ["private tool trace"],
                                "case_id": f"private-{index}",
                                "raw_response": "private response",
                            }
                        ]
                    },
                )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        entries = (await client.get("/api/v1/public/activity")).json()["entries"]
        activity = next(
            entry for entry in entries if entry["agent_id"] == str(agent_id)
        )
        assert activity["status"] == "below_score_floor"
        assert activity["score_count"] == 2
        assert activity["validator_queue_rank"] == 1

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        assert pipeline["status"] == "below_score_floor"
        assert pipeline["score_count"] == 2
        assert pipeline["score_floor"] == pytest.approx(0.80)
        assert len(pipeline["provisional_scores"]) == 2
        case_results = [
            score["case_results"][0] for score in pipeline["provisional_scores"]
        ]
        assert {case["score"] for case in case_results} == {0.10, 0.20}
        for case in case_results:
            assert set(case) == {
                "category",
                "kind",
                "score",
                "correct",
                "latency_ms",
                "notes",
            }
            assert case["category"] == "temporal_reasoning"
            assert case["kind"] == "memory"
            assert case["correct"] is False
            assert case["latency_ms"] == 500
            assert case["notes"] == ["no deterministic value match"]
        for leaked in (
            '"expected"',
            '"called"',
            '"case_id"',
            '"raw_response"',
            "private answer key",
            "private tool trace",
            "private response",
        ):
            assert leaked not in json.dumps(pipeline)

        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_A,
                    bench_version=_ERA,
                    status=TicketStatus.ISSUED,
                    issued_at=now,
                    deadline=now + timedelta(minutes=30),
                )
            )

        entries = (await client.get("/api/v1/public/activity")).json()["entries"]
        activity = next(
            entry for entry in entries if entry["agent_id"] == str(agent_id)
        )
        assert activity["status"] == "waiting_validator"
        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        assert pipeline["status"] == "waiting_validator"

    async def test_score_floor_names_the_agent_whose_composite_it_is(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A miner told "below the floor" must be able to check the floor.

        The number alone is unfalsifiable: the floor is cut by ``composite``
        while the public board's ``rank`` is cut by ``official_composite``, so
        "fifth place" names two different agents on two surfaces. The pipeline
        therefore attributes the number to the row it came from.
        """
        floor_agent_ids = []
        for rank, marker in enumerate("ABCDE"):
            composite = 0.80 + (4 - rank) * 0.01
            floor_agent_ids.append(
                UUID(
                    await _seed_k3(
                        session_maker,
                        miner="5" + marker * 47,
                        composites=[composite, composite, composite],
                    )
                )
            )
        fifth_place_agent_id = floor_agent_ids[-1]

        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        async with session_maker() as session, session.begin():
            for index, (validator, composite) in enumerate(
                ((_VALIDATOR_C, 0.10), (_MINER_B, 0.20))
            ):
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=validator,
                    bench_version=_ERA,
                    run_id=f"attributed-floor-{index}",
                    seed=42,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=500,
                    n=114,
                    generated_at=datetime.now(UTC),
                    signature="ab" * 64,
                )
        _install_db(app, session_maker)

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        assert pipeline["status"] == "below_score_floor"
        assert pipeline["score_floor"] == pytest.approx(0.80)
        assert pipeline["score_floor_agent_id"] == str(fifth_place_agent_id)
        assert pipeline["score_floor_agent_name"] == "agent"
        assert pipeline["score_floor_agent_version"] is None

        # The attribution is checkable: that agent's own record reports the
        # same number the floor quotes.
        floor_holder = (
            await client.get(f"/api/v1/public/agent/{fifth_place_agent_id}/pipeline")
        ).json()
        assert floor_holder["final_composite"] == pytest.approx(pipeline["score_floor"])

    async def test_score_floor_attribution_is_null_below_five_ranked_agents(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """No fifth place, no floor, and no agent credited with one."""
        for marker in "ABCD":
            await _seed_k3(
                session_maker,
                miner="5" + marker * 47,
                composites=[0.80, 0.80, 0.80],
            )
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        _install_db(app, session_maker)

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        assert pipeline["score_floor"] == pytest.approx(0.0)
        assert pipeline["score_floor_agent_id"] is None
        assert pipeline["score_floor_agent_name"] is None
        assert pipeline["score_floor_agent_version"] is None

    async def test_score_floor_holder_is_the_board_row_at_rank_five(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The support report, resolved: "fifth place" names ONE agent.

        This is the same board the divergence was reproduced on -- deliberately
        built so the ``composite`` order and the ``official_composite`` order
        invert -- but both surfaces now cut it with the one canonical ordering
        (:mod:`ditto.score_order`) on the one canonical score. So the floor a
        miner is told he is below is the score of the row the board ranks
        fifth, held by the agent the board shows there, and looking it up
        confirms the gate instead of contradicting it.

        The old behaviour is asserted absent, not merely different: cutting the
        floor on the raw ``composite`` would have picked "E" at 0.82.
        """
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        # Six finalized owners, descending by composite. "E" holds fifth.
        by_marker = {}
        for rank, marker in enumerate("ABCDEF"):
            composite = 0.90 - rank * 0.02
            by_marker[marker] = await _seed_k3(
                session_maker,
                miner="5" + marker * 47,
                composites=[composite, composite, composite],
                details={"bench_version": _ERA},
                created_at=datetime(2026, 6, 1 + rank, tzinfo=UTC),
            )
        fifth_by_composite = by_marker["E"]  # composite 0.82

        # "F" (last by composite, 0.80) completes waves at 0.95, so its
        # official_composite becomes 0.875 and it climbs to third on the board.
        # That pushes every row below it down one, so rank 5 becomes "D".
        async with session_maker() as s, s.begin():
            now = datetime.now(UTC)
            s.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities=_scorer_capabilities(now, versions=[_ERA]),
                )
            )
            # A wave only counts once every cohort member has scored that seed,
            # so all six retest. Everyone but "F" retests at its own composite,
            # leaving its official_composite exactly where it was.
            await append_confirmation_scores(
                s,
                rows=[
                    ConfirmationSeedScore(
                        UUID(agent),
                        "5V1",
                        seed,
                        0.95 if marker == "F" else 0.90 - index * 0.02,
                        f"r-{marker}-{seed}",
                        None,
                    )
                    for index, (marker, agent) in enumerate(by_marker.items())
                    for seed in (100, 200, 300)
                ],
                bench_version=_ERA,
                created_at=now,
            )

        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        async with session_maker() as session, session.begin():
            for index, (validator, composite) in enumerate(
                ((_VALIDATOR_C, 0.10), (_MINER_B, 0.20))
            ):
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=validator,
                    bench_version=_ERA,
                    run_id=f"divergent-floor-{index}",
                    seed=42,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=500,
                    n=114,
                    generated_at=datetime.now(UTC),
                    signature="ab" * 64,
                    details={"bench_version": _ERA},
                )
        _install_db(app, session_maker)

        board = (await client.get("/api/v1/public/leaderboard")).json()
        assert board["continual_aggregate_active"] is True
        entries = board["entries"]
        rank_five = next(entry for entry in entries if entry["rank"] == 5)

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        assert pipeline["status"] == "below_score_floor"

        # The two keys genuinely invert on this board, or the test proves
        # nothing: "F" is last by composite and third by official_composite.
        finalized = [entry for entry in entries if entry["rank"] is not None]
        assert [entry["agent_id"] for entry in finalized] != [
            entry["agent_id"]
            for entry in sorted(finalized, key=lambda entry: -entry["composite"])
        ]

        # THE INVARIANT: one ordering, one score, one fifth place.
        assert pipeline["score_floor_agent_id"] == rank_five["agent_id"]
        assert pipeline["score_floor"] == pytest.approx(rank_five["official_composite"])
        assert pipeline["score_floor_agent_name"] == "agent"

        # And it is not the row the retired raw-composite cut would have named.
        assert pipeline["score_floor_agent_id"] != fifth_by_composite
        assert pipeline["score_floor"] == pytest.approx(0.84)

    async def test_public_progress_never_combines_benchmark_eras(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """love-v8's two eras are one score in each era, not two.

        The older era here is a *retired* one, which is the only shape this
        state has left in production: the floor stops anything below
        ``MIN_SCOREABLE_BENCH_VERSION`` being written or re-leased, but the
        grandfathered rows -- and the lease that was live when the floor
        landed -- still have to be projected in their own era rather than
        folded into the live one.
        """
        await _seed_top_five_floor(session_maker, fifth_place=0.80)
        now = datetime.now(UTC)
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                name="love-v8",
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=now - timedelta(hours=2),
            )
        )
        deadline = now + timedelta(minutes=30)
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
            session_maker() as session,
            session.begin(),
        ):
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_PREV_ERA,
                    desired_version=_ERA,
                    status="activated",
                    cohort_size=5,
                    created_at=now - timedelta(hours=1),
                    activated_at=now,
                )
            )
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_MINER_A,
                    software_version="1.2.3",
                    protocol_version=4,
                    code_digest="ab" * 32,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress={
                        "stage": "running_benchmark",
                        "completed": 10,
                        "total": 119,
                        "ticket_deadline": deadline.isoformat(),
                    },
                    benchmark_progress_reported=True,
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_A,
                    bench_version=_PREV_ERA,
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(seconds=1),
                    deadline=deadline,
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey="5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    bench_version=_ERA,
                    status=TicketStatus.EXPIRED,
                    issued_at=now - timedelta(minutes=30),
                    deadline=now - timedelta(minutes=15),
                    retry_after=now - timedelta(minutes=5),
                    attempt_count=2,
                    manual_retry_grants=1,
                )
            )
            for bench_version, validator, composite in (
                (_PREV_ERA, _VALIDATOR_C, 0.391235),
                (_ERA, _MINER_B, 0.391897),
            ):
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=validator,
                    bench_version=bench_version,
                    run_id=f"love-v8-v{bench_version}",
                    seed=42,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=500,
                    n=119,
                    generated_at=now,
                    signature="ab" * 64,
                    details={"bench_version": bench_version},
                )
        _install_db(app, session_maker)

        activity_body = (await client.get("/api/v1/public/activity")).json()
        activity = next(
            entry
            for entry in activity_body["entries"]
            if entry["agent_id"] == str(agent_id)
        )
        assert activity["status"] == "not_queued"
        assert activity["score_count"] == 1
        assert activity["provisional_composite"] == pytest.approx(0.391897)
        assert activity["validator_queue_rank"] is None
        assert activity["retry_state"] is None
        assert activity_body["status_counts"]["not_queued"] == 1
        assert [work["bench_version"] for work in activity["active_benchmarks"]] == [
            _PREV_ERA
        ]

        operations = (await client.get("/api/v1/public/operations")).json()
        assert operations["active_bench_version"] == _ERA
        assert not any(
            entry["agent_id"] == str(agent_id)
            for entry in operations["activity"]["entries"]
        )
        assert operations["activity"]["status_counts"]["not_queued"] == 1

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        assert pipeline["active_bench_version"] == _ERA
        assert pipeline["status"] == "not_queued"
        assert pipeline["score_count"] == 1
        assert pipeline["score_floor"] == pytest.approx(0.80)
        scores_by_version = {
            score["bench_version"]: score["composite"]
            for score in pipeline["provisional_scores"]
        }
        assert scores_by_version[_PREV_ERA] == pytest.approx(0.391235)
        assert scores_by_version[_ERA] == pytest.approx(0.391897)
        running_by_version = {
            attempt["bench_version"]: attempt["actively_running"]
            for attempt in pipeline["validation_attempts"]
        }
        assert running_by_version[_PREV_ERA] is True

    async def test_retry_state_surfaces_exhausted_and_cooling_submissions(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The public feed labels why a below-quorum submission is (not) advancing."""
        now = datetime.now(UTC)
        exhausted_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                name="exhausted",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        cooling_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_B,
                status=AgentStatus.EVALUATING,
                name="cooling",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        # A rejected submission with the exact same exhausted tickets must NOT be
        # labelled: retry_state is only meaningful while EVALUATING. (Regression
        # guard: the classifier once labelled every status.)
        rejected_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.REJECTED,
                name="rejected",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        cooldown_until = now + timedelta(hours=6)
        async with session_maker() as session, session.begin():
            for agent_id in (exhausted_id, rejected_id):
                for index in range(3):
                    session.add(
                        ValidatorTicket(
                            agent_id=agent_id,
                            validator_hotkey=f"validator-{index}",
                            status=TicketStatus.EXPIRED,
                            issued_at=now - timedelta(hours=3),
                            deadline=now - timedelta(hours=2, minutes=index),
                            bench_version=_ERA,
                            attempt_count=2,
                            manual_retry_grants=0,
                            retry_after=now - timedelta(hours=1),
                        )
                    )
            session.add(
                ValidatorTicket(
                    agent_id=cooling_id,
                    validator_hotkey="validator-0",
                    status=TicketStatus.EXPIRED,
                    issued_at=now - timedelta(hours=1),
                    deadline=now - timedelta(minutes=30),
                    bench_version=_ERA,
                    attempt_count=1,
                    manual_retry_grants=0,
                    infra_retry_grants=1,
                    retry_after=cooldown_until,
                )
            )
        _install_db(app, session_maker)

        by_id = {
            entry["agent_id"]: entry
            for entry in (await client.get("/api/v1/public/operations")).json()[
                "activity"
            ]["entries"]
        }
        assert by_id[str(exhausted_id)]["retry_state"] == "exhausted"
        assert by_id[str(exhausted_id)]["retry_after"] is None
        assert by_id[str(cooling_id)]["retry_state"] == "cooling_down"
        assert (
            datetime.fromisoformat(by_id[str(cooling_id)]["retry_after"])
            == cooldown_until
        )
        # Rejected history is intentionally omitted from the live board snapshot;
        # the complete Activity feed still exposes it without a retry label.
        assert str(rejected_id) not in by_id
        rejected = (
            await client.get("/api/v1/public/activity", params={"q": str(rejected_id)})
        ).json()["entries"][0]
        assert rejected["retry_state"] is None
        assert rejected["retry_after"] is None

    async def test_secondary_multislot_work_is_reported_as_scoring(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Every active capacity slot drives the submission lifecycle label."""
        primary_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                name="primary-slot-agent",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        secondary_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_B,
                status=AgentStatus.EVALUATING,
                name="sm118",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=30)

        def progress(completed: int) -> dict:
            return {
                "stage": "running_benchmark",
                "completed": completed,
                "total": 351,
                "ticket_deadline": deadline.isoformat(),
            }

        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATOR_C,
                    software_version="0.43.13",
                    protocol_version=18,
                    code_digest="ab" * 32,
                    state="running_benchmark",
                    active_agent_id=primary_id,
                    benchmark_progress=progress(210),
                    benchmark_progress_reported=True,
                    benchmark_capacity={
                        "configured_slots": 2,
                        "healthy_slots": ["slot-0", "slot-1"],
                        "admission": "accepting",
                        "active": [
                            {
                                "slot_id": "slot-0",
                                "agent_id": str(primary_id),
                                "bench_version": _ERA,
                                "progress": progress(210),
                            },
                            {
                                "slot_id": "slot-1",
                                "agent_id": str(secondary_id),
                                "bench_version": _ERA,
                                "progress": progress(197),
                            },
                        ],
                    },
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                )
            )
            for slot_id, agent_id in (
                ("slot-0", primary_id),
                ("slot-1", secondary_id),
            ):
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=_VALIDATOR_C,
                        slot_id=slot_id,
                        bench_version=_ERA,
                        status=TicketStatus.ISSUED,
                        issued_at=now - timedelta(seconds=1),
                        deadline=deadline,
                    )
                )
        _install_db(app, session_maker)

        activity = (await client.get("/api/v1/public/activity")).json()
        activity_by_id = {entry["agent_id"]: entry for entry in activity["entries"]}
        secondary = activity_by_id[str(secondary_id)]
        assert secondary["status"] == "evaluating"
        assert secondary["validator_queue_rank"] is None
        assert [work["slot_id"] for work in secondary["active_benchmarks"]] == [
            "slot-1"
        ]
        assert secondary["active_benchmarks"][0]["completed_checks"] == 197

        operations = (await client.get("/api/v1/public/operations")).json()
        operations_by_id = {
            entry["agent_id"]: entry for entry in operations["activity"]["entries"]
        }
        assert operations_by_id[str(secondary_id)]["status"] == "evaluating"
        assert operations["activity"]["status_counts"]["evaluating"] == 2

    async def test_progress_is_multi_validator_allowlisted_and_recursively_redacted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                name="privacy-safe-agent",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=30)
        safe_progress = {
            "stage": "running_benchmark",
            "completed": 51,
            "total": 114,
            "ticket_deadline": deadline.isoformat(),
        }
        sentinel = "PRIVATE_PROMPT_CANARY_DO_NOT_PUBLISH"
        async with session_maker() as session, session.begin():
            for hotkey, progress in (
                (_MINER_A, safe_progress),
                (_MINER_B, {**safe_progress, "completed": 3, "total": 8}),
                (_VALIDATOR_C, {**safe_progress, "prompt": sentinel}),
            ):
                session.add(
                    ValidatorHeartbeat(
                        validator_hotkey=hotkey,
                        software_version="1.2.3",
                        protocol_version=4,
                        code_digest="ab" * 32,
                        state="running_benchmark",
                        active_agent_id=agent_id,
                        benchmark_progress=progress,
                        benchmark_progress_reported=True,
                        reported_at=now,
                        seen_at=now,
                        signature="cd" * 64,
                    )
                )
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        bench_version=_ERA,
                        status=TicketStatus.ISSUED,
                        issued_at=now - timedelta(seconds=1),
                        deadline=deadline,
                    )
                )
        _install_db(app, session_maker)

        responses = [
            await client.get("/api/v1/public/validators"),
            await client.get("/api/v1/public/activity"),
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline"),
        ]
        assert all(response.status_code == 200 for response in responses)
        public_progress_keys = {
            "slot_id",
            "agent_id",
            "agent_name",
            "bench_version",
            "started_at",
            "stage",
            "completed_checks",
            "total_checks",
            "percent",
            "stalled",
        }
        fleet = responses[0].json()
        shown = [
            row["active_benchmark"]
            for row in fleet["validators"]
            if row["active_benchmark"] is not None
        ]
        assert len(shown) == 2
        assert all(set(progress) == public_progress_keys for progress in shown)
        first = next(
            progress for progress in shown if progress["completed_checks"] == 51
        )
        assert first["percent"] == 44  # 51/114 exactly, no 5% bucket.
        assert first["bench_version"] == _ERA
        assert first["total_checks"] == 114
        assert datetime.fromisoformat(first["started_at"].replace("Z", "+00:00")) == (
            now - timedelta(seconds=1)
        )
        threshold = next(
            progress for progress in shown if progress["completed_checks"] == 3
        )
        assert threshold["percent"] == 37  # 3/8 = 37.5%, truncated, not bucketed.
        activity = responses[1].json()["entries"][0]
        assert len(activity["active_benchmarks"]) == 2
        pipeline = responses[2].json()
        assert sum(a["actively_running"] for a in pipeline["validation_attempts"]) == 2
        assert all(a["bench_version"] == _ERA for a in pipeline["validation_attempts"])

        forbidden_keys = {
            "case_id",
            "case_category",
            "prompt",
            "expected",
            "called",
            "tool_names",
            "memory_contents",
            "dataset",
            "dataset_sha256",
            "seed",
            "canary",
            "partial_score",
            "latency_ms",
            "model_output",
            "harness_logs",
            "tarball_logs",
            "run_id",
            "container_id",
            "filesystem_path",
            "ip_address",
            "error_body",
            "ticket_deadline",
        }

        def assert_redacted(value: object) -> None:
            if isinstance(value, dict):
                assert forbidden_keys.isdisjoint(value)
                for nested in value.values():
                    assert_redacted(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_redacted(nested)
            elif isinstance(value, str):
                assert sentinel not in value

        for response in responses:
            assert_redacted(response.json())

    async def test_per_case_notes_are_a_closed_vocabulary_not_scorer_free_text(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The Go scorers interpolate DATASET content into several per-case notes:
        # the memory grader embeds the distractor value it matched, and the
        # trajectory scorer embeds required/forbidden argument names. Those notes
        # must not reach an unauthenticated, CDN-cached response — least of all
        # `provisional_scores`, which is served BEFORE the /agent/{id}/dataset
        # reveal gate that holds answer keys until a run is finalized.
        distractor = "DISTRACTOR_CANARY_DO_NOT_PUBLISH"
        arg_key = "ARGKEY_CANARY_DO_NOT_PUBLISH"
        forbidden_arg = "FORBIDDENARG_CANARY_DO_NOT_PUBLISH"
        misrouted_tool = "TOOLNAME_CANARY_DO_NOT_PUBLISH"
        future_note = "EXPECTED_ANSWER_CANARY_DO_NOT_PUBLISH"
        rogue_kind = "canary_leak"
        sentinels = (
            distractor,
            arg_key,
            forbidden_arg,
            misrouted_tool,
            future_note,
            rogue_kind,
        )
        planted_notes = [
            # Mechanical notes that must SURVIVE, verbatim.
            "deterministic value match",
            "1 extra/unexpected tool call(s)",
            "capped: observable case not executed via tool_endpoint "
            "(self-report untrusted)",
            "judged correct=false grounded=true",
            # Value-bearing notes: the verdict survives, the value must not.
            f'surfaced a wrong same-attribute value "{distractor}" (scored 0)',
            f"wrong value for arg {arg_key}",
            f"forbidden arg present: {forbidden_arg}",
            f"misrouted a memory request to a non-memory tool: {misrouted_tool}",
            # A note a future scorer might add, and a rogue AnswerKind smuggled
            # through an otherwise-known template: both dropped by default.
            f"expected answer was {future_note}",
            f"deterministic {rogue_kind} match",
        ]
        details = {
            "bench_version": _ERA,
            "per_case": [
                {
                    "kind": "memory",
                    "category": "temporal_reasoning",
                    "score": 0.5,
                    "correct": False,
                    "latency_ms": 500,
                    "notes": planted_notes,
                }
            ],
        }

        # Finalized (k=3) — reaches /leaderboard and /agent/{id}/scores.
        finalized_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6],
            details=details,
        )
        # Provisional (accepted, pre-quorum) — reaches /pipeline provisional_scores.
        provisional_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_B,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        async with session_maker() as session, session.begin():
            await upsert_score(
                session,
                agent_id=provisional_id,
                validator_hotkey=_VALIDATOR_C,
                bench_version=_ERA,
                run_id="provisional-notes",
                seed=42,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=500,
                n=114,
                generated_at=datetime.now(UTC),
                signature="ab" * 64,
                details=details,
            )
        _install_db(app, session_maker)

        responses = [
            await client.get("/api/v1/public/leaderboard"),
            await client.get("/api/v1/public/activity"),
            await client.get(f"/api/v1/public/agent/{provisional_id}/pipeline"),
            await client.get(f"/api/v1/public/agent/{finalized_id}/scores"),
        ]
        assert all(response.status_code == 200 for response in responses)
        # No planted dataset value appears anywhere, on any endpoint.
        for response in responses:
            for sentinel in sentinels:
                assert sentinel not in response.text

        published = (
            await client.get(f"/api/v1/public/agent/{provisional_id}/pipeline")
        ).json()["provisional_scores"][0]["case_results"][0]["notes"]
        assert published == [
            "deterministic value match",
            "1 extra/unexpected tool call(s)",
            "capped: observable case not executed via tool_endpoint "
            "(self-report untrusted)",
            "judged correct=false grounded=true",
            # The verdicts are kept; the values they were rendered around are gone.
            "surfaced a wrong same-attribute value (scored 0)",
            "wrong value for a required arg",
            "forbidden arg present",
            "misrouted a memory request to a non-memory tool",
        ]
        # The finalized projection publishes exactly the same closed vocabulary.
        assert responses[3].json()["scores"][0]["case_results"][0]["notes"] == published

    async def test_live_work_marks_only_its_own_bench_version_attempt(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=30)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_MINER_A,
                    software_version="1.2.3",
                    protocol_version=4,
                    code_digest="ab" * 32,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress={
                        "stage": "running_benchmark",
                        "completed": 8,
                        "total": 119,
                        "ticket_deadline": deadline.isoformat(),
                    },
                    benchmark_progress_reported=True,
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                )
            )
            # The same validator already finished this agent on the two older
            # eras; only the newest ticket is live.
            live_version = _NEXT_ERA + 1
            for bench_version, status in (
                (_ERA, TicketStatus.SCORED),
                (_NEXT_ERA, TicketStatus.SCORED),
                (live_version, TicketStatus.ISSUED),
            ):
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=_MINER_A,
                        bench_version=bench_version,
                        status=status,
                        issued_at=now - timedelta(seconds=1),
                        deadline=deadline,
                    )
                )
        _install_db(app, session_maker)

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        running = [
            attempt
            for attempt in pipeline["validation_attempts"]
            if attempt["actively_running"]
        ]
        assert [attempt["bench_version"] for attempt in running] == [live_version]
        assert running[0]["benchmark_progress"]["bench_version"] == live_version
        assert all(
            attempt["benchmark_progress"] is None
            for attempt in pipeline["validation_attempts"]
            if attempt["bench_version"] != live_version
        )

    async def test_delayed_legacy_or_omitted_progress_cannot_revive_reissued_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        issued_at = now - timedelta(seconds=5)
        old_signed_at = now - timedelta(seconds=10)
        deadline = now + timedelta(minutes=30)
        async with session_maker() as session, session.begin():
            for hotkey, protocol_version in ((_MINER_A, 3), (_MINER_B, 4)):
                session.add(
                    ValidatorHeartbeat(
                        validator_hotkey=hotkey,
                        software_version="1.2.3",
                        protocol_version=protocol_version,
                        code_digest="ab" * 32,
                        state="running_benchmark",
                        active_agent_id=agent_id,
                        benchmark_progress=None,
                        benchmark_progress_reported=False,
                        reported_at=old_signed_at,
                        # Receipt after reissue must not make the old signature fresh.
                        seen_at=now,
                        signature="cd" * 64,
                    )
                )
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        bench_version=_ERA,
                        status=TicketStatus.ISSUED,
                        issued_at=issued_at,
                        deadline=deadline,
                    )
                )
        _install_db(app, session_maker)

        fleet = (await client.get("/api/v1/public/validators")).json()
        assert all(row["active_agent_id"] is None for row in fleet["validators"])
        activity = (await client.get("/api/v1/public/activity")).json()
        assert activity["entries"][0]["status"] == "waiting_validator"
        assert activity["entries"][0]["active_benchmarks"] == []

    async def test_respects_limit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, miner=_MINER_A)
        await _seed_agent(session_maker, miner=_MINER_B)
        _install_db(app, session_maker)
        body = (await client.get("/api/v1/public/activity?limit=1")).json()
        assert body["count"] == 1
        assert body["total"] == 2
        assert body["total_pages"] == 2

    async def test_paginates_newest_first_without_overlap(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        for hour, name in ((10, "oldest"), (11, "middle"), (12, "newest")):
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                name=name,
                created_at=datetime(2026, 7, 13, hour, tzinfo=UTC),
            )
        _install_db(app, session_maker)

        first = (await client.get("/api/v1/public/activity?limit=2&page=1")).json()
        second = (await client.get("/api/v1/public/activity?limit=2&page=2")).json()

        assert [entry["name"] for entry in first["entries"]] == ["newest", "middle"]
        assert [entry["name"] for entry in second["entries"]] == ["oldest"]
        assert first["total"] == second["total"] == 3
        assert first["total_pages"] == second["total_pages"] == 2
        assert first["page"] == 1
        assert second["page"] == 2

    async def test_exposes_progress_count_with_partial_score(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.42],
            status=AgentStatus.EVALUATING,
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        resp = await client.get("/api/v1/public/activity")
        entry = resp.json()["entries"][0]
        assert entry["score_count"] == 1
        assert entry["quorum"] == 3
        assert entry["provisional_composite"] == pytest.approx(0.42)
        assert "signature" not in resp.text

    @pytest.mark.parametrize("score_count", [0, 1, 2, 3])
    async def test_pipeline_exposes_only_safe_accepted_scores_before_quorum(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        score_count: int,
    ) -> None:
        composites = [0.41, 0.58, 0.73][:score_count]
        transcript_sha256 = "ef" * 32
        if score_count:
            agent_id = await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=composites,
                status=(
                    AgentStatus.SCORED if score_count == 3 else AgentStatus.EVALUATING
                ),
                details={
                    "bench_version": _ERA,
                    "transcript_sha256": transcript_sha256,
                },
            )
        else:
            agent_id = await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        body = response.json()
        assert body["score_count"] == score_count
        assert body["quorum"] == 3
        assert len(body["provisional_scores"]) == score_count
        assert body["final_composite"] == (
            pytest.approx(0.58) if score_count == 3 else None
        )
        assert sorted(score["composite"] for score in body["provisional_scores"]) == (
            composites
        )
        for score in body["provisional_scores"]:
            assert score["seed"] == "987654321"
            assert score["run_size"] == "full"
            assert score["bench_version"] == _ERA
            assert score["datagen_version"] == "v0.12.0"
            assert score["seed_source"] == "on_chain"
            assert score["dataset_sha256"] == "cd" * 32
            assert score["reproduction_command"] == (
                "go run github.com/ditto-assistant/dittobench-datagen/cmd/"
                f"generate@v0.12.0 -bench-version {_ERA} -seed 987654321 "
                "-run-size full -out dataset.json"
            )
            assert score["verification_command"].endswith(
                "-seed 987654321 -run-size full -sha"
            )
            # The signature-bound transcript digest is public; the offline
            # verification path depends on it.
            assert score["transcript_sha256"] == transcript_sha256
            assert "validator_hotkey" not in score
            assert "signature" not in score
            assert "ticket_deadline" not in score
            assert "run_id" not in score

    async def test_pipeline_labels_random_seed_fallback_without_block_provenance(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.52],
            status=AgentStatus.EVALUATING,
            dataset_seed_block=None,
            dataset_seed_block_hash=None,
            details={"bench_version": _ERA},
        )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        assert body["provisional_scores"][0]["seed_source"] == "random_fallback"

    async def test_pipeline_labels_validator_local_seed_without_pinned_dataset(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """No pinned dataset at all (generation disabled when screened)."""
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.52],
            status=AgentStatus.EVALUATING,
            dataset_seed=None,
            dataset_sha256=None,
            dataset_run_size=None,
            dataset_seed_block=None,
            dataset_seed_block_hash=None,
            details={"bench_version": _ERA},
        )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        score = body["provisional_scores"][0]
        assert score["seed_source"] == "validator_local"
        assert score["run_size"] is None
        assert score["dataset_sha256"] is None
        assert score["reproduction_command"] is None
        assert score["verification_command"] is None

    async def test_pipeline_keeps_accepted_score_visible_during_retry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.52],
                status=AgentStatus.EVALUATING,
                details={"bench_version": _ERA},
            )
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_B,
                    bench_version=_ERA,
                    status=TicketStatus.EXPIRED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    issued_at=now - timedelta(hours=2),
                    deadline=now - timedelta(hours=1),
                    failure_reason="sandbox_oom",
                    failed_at=now - timedelta(hours=1),
                )
            )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        assert body["score_count"] == 1
        assert body["provisional_scores"][0]["composite"] == pytest.approx(0.52)
        assert body["validation_attempts"][0]["status"] == "expired"
        assert body["validation_attempts"][0]["bench_version"] == _ERA
        assert body["validation_attempts"][0]["purpose"] == "canonical_quorum"
        assert body["validation_attempts"][0]["failure_reason"] == "sandbox_oom"
        assert body["validation_attempts"][0]["failed_at"] is not None
        assert body["validation_attempts"][0]["attempt_count"] == 1

    async def test_pipeline_publishes_run_cost_and_allowance_exhaustion(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Publish aggregate spend and an allowlisted terminal agent cause.

        The diagnostic detail remains private. Only the exact typed allowance
        code is public, alongside the platform's durable aggregate meter for
        the validator lease that consumed it.
        """
        agent_id = UUID(
            await _seed_agent(
                session_maker,
                miner=_MINER_A,
                status=AgentStatus.EVALUATING,
                name="allowance-hungry-agent",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        now = datetime.now(UTC)
        deadline = now - timedelta(minutes=5)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_B,
                    slot_id="slot-0",
                    bench_version=_ERA,
                    status=TicketStatus.EXPIRED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    issued_at=now - timedelta(hours=1),
                    deadline=deadline,
                    failure_reason="scoring_error",
                    failure_detail="inference_allowance_exhausted",
                    failed_at=deadline,
                )
            )
            await session.flush()
            session.add(
                InferenceGrant(
                    grant_id=uuid4(),
                    agent_id=agent_id,
                    bench_version=_ERA,
                    validator_hotkey=_MINER_B,
                    slot_id="slot-0",
                    ticket_deadline=deadline,
                    expires_at=deadline,
                    status="exhausted",
                    generation=1,
                    allowed_models=["qwen/qwen3-32b"],
                    request_budget=8192,
                    request_count=8192,
                    token_budget=25_000_000,
                    prompt_tokens=7_000_000,
                    completion_tokens=500_000,
                    cost_microusd=1_234_567,
                    embedding_model="perplexity/pplx-embed-v1-0.6b",
                    embedding_profile="dittobench-v8-pplx-embed-v1-0.6b-768-v1",
                    embedding_provider="Perplexity",
                    embedding_dimensions=768,
                    embedding_request_budget=10_000,
                    embedding_request_count=123,
                    embedding_token_budget=5_000_000,
                    embedding_tokens=456_789,
                    embedding_cost_microusd=12_345,
                    usage_accounting_version=2,
                    created_at=now - timedelta(hours=1),
                    updated_at=deadline,
                )
            )
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        body = response.json()
        attempt = body["validation_attempts"][0]
        assert attempt["failure_reason"] == "scoring_error"
        assert attempt["failure_code"] == "inference_allowance_exhausted"
        run = body["inference_runs"][0]
        assert run == {
            "validator_hotkey": _MINER_B,
            "bench_version": _ERA,
            "ticket_deadline": deadline.isoformat().replace("+00:00", "Z"),
            "status": "exhausted",
            "request_budget": 8192,
            "requests": 8192,
            "prompt_tokens": 7_000_000,
            "completion_tokens": 500_000,
            "token_budget": 25_000_000,
            "embedding_requests": 123,
            "embedding_tokens": 456_789,
            "cost_microusd": 1_246_912,
            "accounting_version": 2,
            "created_at": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "updated_at": deadline.isoformat().replace("+00:00", "Z"),
        }

    async def test_pipeline_dates_a_retried_lease_after_its_kept_failure(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A scored lease keeps its old failure, so publish what supersedes it.

        ``failure_reason``/``failed_at`` survive a reissue by design (retry
        accounting and audit), so a ticket that failed, was re-leased, and then
        scored still carries them. Consumers can only tell that the failure is
        history from ``issued_at`` moving past ``failed_at`` and from
        ``attempt_count`` -- both must be on the wire.
        """
        agent_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.84],
                status=AgentStatus.EVALUATING,
                details={"bench_version": _ERA},
            )
        )
        now = datetime.now(UTC)
        failed_at = now - timedelta(hours=15)
        reissued_at = now - timedelta(minutes=40)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_B,
                    bench_version=_ERA,
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    issued_at=reissued_at,
                    deadline=now + timedelta(hours=1),
                    failure_reason="scoring_error",
                    failed_at=failed_at,
                    attempt_count=2,
                )
            )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()
        attempt = next(
            row
            for row in body["validation_attempts"]
            if row["validator_hotkey"] == _MINER_B
        )

        assert attempt["status"] == "scored"
        assert attempt["attempt_count"] == 2
        # The kept failure stays readable, but strictly behind the lease that
        # replaced it -- that ordering is what marks it as history.
        assert attempt["failure_reason"] == "scoring_error"
        assert datetime.fromisoformat(attempt["failed_at"]) < datetime.fromisoformat(
            attempt["issued_at"]
        )

    async def test_pipeline_separates_canonical_quorum_from_continual_retests(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.db.queries.confirmation_scores import (
            ConfirmationSeedScore,
            append_confirmation_scores,
        )

        agent_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.91, 0.92, 0.93],
                status=AgentStatus.SCORED,
                details={"bench_version": _ERA},
                # This test writes its own continual-retest tickets on the same
                # composite key, so the helper must not also seed the canonical
                # quorum tickets.
                accepted_tickets=False,
            )
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            canonical = list(
                await session.scalars(
                    select(Score)
                    .where(Score.agent_id == agent_id)
                    .order_by(Score.validator_hotkey)
                )
            )
            for score in canonical:
                score.created_at = now - timedelta(hours=1)
            completed_validator = canonical[0].validator_hotkey
            pending_validator = canonical[1].validator_hotkey
            replacement_validator = canonical[2].validator_hotkey
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        agent_id=agent_id,
                        validator_hotkey=completed_validator,
                        seed=111,
                        composite=0.94,
                        run_id="confirmation-run",
                        signature="ab" * 64,
                    ),
                    ConfirmationSeedScore(
                        agent_id=agent_id,
                        validator_hotkey=completed_validator,
                        seed=222,
                        composite=0.95,
                        run_id="confirmation-run",
                        signature="ab" * 64,
                    ),
                ],
                bench_version=_ERA,
                created_at=now,
            )
            session.add_all(
                [
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=completed_validator,
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CONTINUAL_RETEST,
                        issued_at=now - timedelta(minutes=10),
                        deadline=now - timedelta(minutes=5),
                        bench_version=_ERA,
                    ),
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=pending_validator,
                        status=TicketStatus.ISSUED,
                        purpose=TicketPurpose.CONTINUAL_RETEST,
                        issued_at=now,
                        deadline=now + timedelta(minutes=30),
                        bench_version=_ERA,
                    ),
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=replacement_validator,
                        status=TicketStatus.ISSUED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        issued_at=now,
                        deadline=now + timedelta(minutes=30),
                        bench_version=_ERA,
                    ),
                ]
            )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        assert body["score_count"] == body["quorum"] == 3
        assert len(body["provisional_scores"]) == 3
        assert [
            (score["seed"], score["composite"]) for score in body["confirmation_scores"]
        ] == [
            ("111", pytest.approx(0.94)),
            ("222", pytest.approx(0.95)),
        ]
        assert all("run_id" not in score for score in body["confirmation_scores"])
        assert {
            attempt["validator_hotkey"]: attempt["purpose"]
            for attempt in body["validation_attempts"]
        } == {
            completed_validator: "continual_retest",
            pending_validator: "continual_retest",
            replacement_validator: "canonical_quorum",
        }

    async def test_pipeline_keeps_mixed_benchmark_quorums_separate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = UUID(
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.41, 0.58, 0.73],
                status=AgentStatus.SCORED,
                details={"bench_version": _ERA},
            )
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=_VALIDATOR_C,
                run_id="next-era-run",
                seed=123,
                composite=0.91,
                tool_mean=0.91,
                memory_mean=0.91,
                median_ms=400,
                n=114,
                generated_at=now,
                signature="ab" * 64,
                details={"bench_version": _NEXT_ERA},
                bench_version=_NEXT_ERA,
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_A,
                    status=TicketStatus.ISSUED,
                    issued_at=now,
                    deadline=now + timedelta(hours=1),
                    bench_version=_NEXT_ERA,
                )
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

        assert response.status_code == 200
        body = response.json()
        assert body["active_bench_version"] == _ERA
        # A ticket for a version being rolled out *ahead* of the active one does
        # not move the reported era. This submission is current-generation work
        # that finished on the active era; its next-era run is visible below,
        # not in the headline.
        assert body["score_bench_version"] == _ERA
        assert body["score_count"] == 3
        assert body["final_composite"] == pytest.approx(0.58)
        assert [score["bench_version"] for score in body["provisional_scores"]].count(
            _ERA
        ) == 3
        assert [score["bench_version"] for score in body["provisional_scores"]].count(
            _NEXT_ERA
        ) == 1
        assert body["validation_attempts"][0]["bench_version"] == _NEXT_ERA

    async def test_pipeline_counts_a_previous_generation_below_quorum_in_its_own_era(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """2 of 3 in a closed generation must not read as 0 of 3.

        The count used to be scoped to the active benchmark unconditionally, so
        every previous-generation detail page answered 0 -- indistinguishable
        from a submission no validator ever picked up, and to the miner who
        earned those scores it read as if the work had been thrown away.

        The closed generation is a retired one now: the floor refuses new
        writes below it, and these grandfathered rows are exactly the history
        the page still has to count.
        """
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            agent_id = await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.44, 0.61],
                status=AgentStatus.EVALUATING,
                details={"bench_version": _PREV_ERA},
            )
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_PREV_ERA,
                    desired_version=_ERA,
                    status="activated",
                    cohort_size=5,
                    created_at=datetime.now(UTC) - timedelta(days=1),
                    activated_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        assert body["active_bench_version"] == _ERA
        assert body["score_bench_version"] == _PREV_ERA
        assert body["score_count"] == 2
        assert body["final_composite"] is None

    async def test_pipeline_keeps_a_previous_generations_finalized_median(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The count and the median name the same era, or the page contradicts.

        Reporting "3 of 3" beside a null final composite would be a new lie in
        place of the old one, so both are answered for the era the submission
        was actually finalized in -- a retired era here, whose grandfathered
        rows the floor keeps readable but refuses to let anyone add to.
        """
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            agent_id = await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.41, 0.58, 0.73],
                status=AgentStatus.SCORED,
                details={"bench_version": _PREV_ERA},
            )
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_PREV_ERA,
                    desired_version=_ERA,
                    status="activated",
                    cohort_size=5,
                    created_at=datetime.now(UTC) - timedelta(days=1),
                    activated_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        assert body["active_bench_version"] == _ERA
        assert body["score_bench_version"] == _PREV_ERA
        assert body["score_count"] == 3
        assert body["final_composite"] == pytest.approx(0.58)

    @pytest.mark.parametrize(
        "status",
        [AgentStatus.SCREENING, AgentStatus.QUARANTINED, AgentStatus.REJECTED],
    )
    async def test_pipeline_preserves_scores_without_finalizing_screening_states(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        status: AgentStatus,
    ) -> None:
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.41, 0.58, 0.73],
            status=status,
            details={"bench_version": _ERA},
        )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")).json()

        assert body["score_count"] == 3
        assert len(body["provisional_scores"]) == 3
        assert body["final_composite"] is None


class TestPublicSubmissionScores:
    async def test_detail_exposes_k3_breakdown_and_median(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.40, 0.70, 0.55]
        )
        _install_db(app, session_maker)

        resp = await client.get(f"/api/v1/public/agent/{agent_id}/scores")
        assert resp.status_code == 200
        assert (
            resp.headers["Cache-Control"]
            == "public, max-age=30, stale-while-revalidate=120"
        )
        body = resp.json()
        assert body["agent_id"] == agent_id
        assert body["miner_hotkey"] == _MINER_A
        assert body["status"] == "scored"
        assert body["quorum"] == 3
        assert body["score_count"] == 3
        # Median of {0.40, 0.55, 0.70} is 0.55 — no single validator controls it.
        assert body["median_composite"] == pytest.approx(0.55)
        # The dataset pin + raw seed are published for reproduction/audit.
        assert body["dataset_seed"] == 987654321
        assert body["dataset_sha256"] == "cd" * 32
        assert body["dataset_run_size"] == "full"
        # The on-chain seed provenance lets anyone verify the seed was not
        # platform-chosen (recompute derive_seed(block_hash, agent_id)).
        assert body["dataset_seed_block"] == 4321
        assert body["dataset_seed_block_hash"] == "0x" + "9f" * 32
        # All three validators, each with hotkey + signature (self-verifying).
        assert len(body["scores"]) == 3
        hotkeys = {s["validator_hotkey"] for s in body["scores"]}
        assert len(hotkeys) == 3
        for s in body["scores"]:
            assert s["signature"] == "ab" * 64
            assert s["seed"] == 987654321
            assert "run_id" in s
            # Scores recorded before lease-bound signing remain public and
            # continue counting; null identifies their legacy signature format.
            assert s["ticket_deadline"] is None
            # No bench_version in details → published as null (legacy), never
            # guessed from the column default.
            assert s["bench_version"] is None

    async def test_detail_labels_each_score_with_its_bench_version(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A re-scored agent carries rows from more than one benchmark version;
        # each published row names the version it was scored under so its
        # incomparable composites cannot be read as one pool.
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.40, 0.70, 0.55],
            details={"bench_version": _ERA},
        )
        async with session_maker() as s, s.begin():
            await upsert_score(
                s,
                agent_id=UUID(agent_id),
                validator_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                run_id="run_next_era",
                seed=987654321,
                composite=0.61,
                tool_mean=0.61,
                memory_mean=0.61,
                median_ms=500,
                n=110,
                generated_at=datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC),
                signature="ab" * 64,
                details={"bench_version": _NEXT_ERA},
                bench_version=_NEXT_ERA,
            )
        _install_db(app, session_maker)

        body = (await client.get(f"/api/v1/public/agent/{agent_id}/scores")).json()

        assert body["score_count"] == 4
        assert sorted(s["bench_version"] for s in body["scores"]) == [
            _ERA,
            _ERA,
            _ERA,
            _NEXT_ERA,
        ]

    async def test_detail_exposes_redacted_per_case_breakdown(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Per-validator per-case breakdown (where points were won/lost) is served,
        # redacted: category/kind/score/pass/latency/notes but never the answer key.
        details = {
            "per_case": [
                {
                    "kind": "tool",
                    "category": "web_search",
                    "score": 0.6,
                    "correct": False,
                    "latency_ms": 3382,
                    "notes": ["1 extra/unexpected tool call(s)"],
                    "expected": ["search_web"],
                    "called": ["search_web", "search_web"],
                    "case_id": "web_search-8860569897825046057-0001",
                },
            ],
        }
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6],
            details=details,
        )
        _install_db(app, session_maker)
        resp = await client.get(f"/api/v1/public/agent/{agent_id}/scores")
        body = resp.json()
        cases = body["scores"][0]["case_results"]
        assert cases and cases[0]["category"] == "web_search"
        assert cases[0]["score"] == pytest.approx(0.6)
        assert cases[0]["correct"] is False
        assert set(cases[0]).issubset(
            {"category", "kind", "score", "correct", "latency_ms", "notes"}
        )
        # The answer key never appears anywhere in the response.
        for leaked in ('"expected"', '"called"', '"case_id"'):
            assert leaked not in resp.text

    async def test_detail_omits_per_case_answer_key(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        _install_db(app, session_maker)
        raw = (await client.get(f"/api/v1/public/agent/{agent_id}/scores")).text
        # The per-submission record publishes validators + seed by design, but
        # still never the per-case answer key.
        for answer_key in ('"expected"', '"called"', '"case_id"', '"per_case"'):
            assert answer_key not in raw

    async def test_detail_404_for_unknown_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        resp = await client.get(f"/api/v1/public/agent/{uuid4()}/scores")
        assert resp.status_code == 404

    async def test_detail_404_for_provisional_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A still-evaluating agent's partial scores must not be exposed.
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4],
            status=AgentStatus.EVALUATING,
        )
        # ...nor a held (suspected-copy) agent's.
        held_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.9, 0.9, 0.9],
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        _install_db(app, session_maker)
        assert (
            await client.get(f"/api/v1/public/agent/{agent_id}/scores")
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/public/agent/{held_id}/scores")
        ).status_code == 404

    async def test_index_lists_recent_finalized_only(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6],
            base_time=datetime(2026, 6, 8, 10, 0, 0, tzinfo=UTC),
        )
        await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.7, 0.8, 0.9],
            base_time=datetime(2026, 6, 8, 14, 0, 0, tzinfo=UTC),
        )
        # Held + still-evaluating must be excluded from the index.
        await _seed_k3(
            session_maker,
            miner="5HeldMinerXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            composites=[0.99, 0.99, 0.99],
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        _install_db(app, session_maker)

        body = (await client.get("/api/v1/public/submissions")).json()
        assert body["count"] == 2
        assert body["quorum"] == 3
        # Most recently scored first: MINER_B (14:00) before MINER_A (10:00).
        assert [s["miner_hotkey"] for s in body["submissions"]] == [_MINER_B, _MINER_A]
        top = body["submissions"][0]
        assert top["median_composite"] == pytest.approx(0.8)
        assert top["score_count"] == 3
        assert top["dataset_seed"] == 987654321
        assert top["last_scored_at"] is not None

    async def test_index_respects_limit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        for i in range(3):
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.4, 0.5, 0.6],
                base_time=datetime(2026, 6, 8, 10 + i, 0, 0, tzinfo=UTC),
            )
        _install_db(app, session_maker)
        body = (await client.get("/api/v1/public/submissions?limit=2")).json()
        assert body["count"] == 2

    async def test_index_empty(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        resp = await client.get("/api/v1/public/submissions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["submissions"] == []


async def _set_score_created_times(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: str,
    created_at: list[datetime],
) -> None:
    async with maker() as session, session.begin():
        scores = list(
            (
                await session.execute(
                    select(Score)
                    .where(Score.agent_id == UUID(agent_id))
                    .order_by(Score.bench_version, Score.validator_hotkey)
                )
            )
            .scalars()
            .all()
        )
        assert len(scores) == len(created_at)
        for score, recorded_at in zip(scores, created_at, strict=True):
            score.created_at = recorded_at


_UNSET = object()


async def _crown(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: str,
    first_crowned_at: datetime,
    weight_confirmed_at: datetime | None | object = _UNSET,
) -> None:
    """Mark an agent as having held the KOTH crown.

    By default the on-chain weight confirmation is stamped at the same instant
    (a fully armed king). Pass ``weight_confirmed_at=None`` for an ever-king that
    has not yet been confirmed on-chain, so its window has not started.
    """
    confirmed = (
        first_crowned_at if weight_confirmed_at is _UNSET else weight_confirmed_at
    )
    async with maker() as session, session.begin():
        session.add(
            AgentKingship(
                agent_id=UUID(agent_id),
                first_crowned_at=first_crowned_at,
                weight_confirmed_at=confirmed,
            )
        )


class TestPublicArtifactRelease:
    async def test_default_releases_the_king_source_after_the_48h_reign_window(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        first_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        second_id = await _seed_k3(
            session_maker, miner=_MINER_B, composites=[0.7, 0.8, 0.9]
        )
        for agent_id in (first_id, second_id):
            await _set_score_created_times(
                session_maker,
                agent_id=agent_id,
                created_at=[
                    now - timedelta(hours=50),
                    now - timedelta(hours=49),
                    now - timedelta(hours=48, minutes=1),
                ],
            )
            # Both agents have held the crown for longer than the 48h window.
            await _crown(
                session_maker,
                agent_id=agent_id,
                first_crowned_at=now - timedelta(hours=48, minutes=1),
            )
        _install_db(app, session_maker)
        storage = AsyncMock()
        storage.presigned_get_url.side_effect = lambda **kwargs: (
            f"https://objects.example/{kwargs['key']}"
        )

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        submissions = (await client.get("/api/v1/public/submissions")).json()
        releases = {
            entry["agent_id"]: entry["artifact_release"]
            for entry in submissions["submissions"]
        }
        assert set(releases) == {first_id, second_id}
        assert all(release["status"] == "available" for release in releases.values())
        assert all(release["embargo_hours"] == 48 for release in releases.values())
        assert all(
            release["download_available"] is True for release in releases.values()
        )

        for agent_id in (first_id, second_id):
            response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "private, no-store"
            body = response.json()
            assert body["agent_id"] == agent_id
            assert body["bench_version"] == _ERA
            assert body["sha256"] == "ab" * 32
            assert body["download_url"].endswith(f"{agent_id}/agent.tar.gz")

        assert {
            call.kwargs["key"] for call in storage.presigned_get_url.await_args_list
        } == {
            f"{first_id}/agent.tar.gz",
            f"{second_id}/agent.tar.gz",
        }
        assert all(
            call.kwargs["expires_in"] == 300
            for call in storage.presigned_get_url.await_args_list
        )
        assert {
            call.kwargs["attachment_filename"]
            for call in storage.presigned_get_url.await_args_list
        } == {
            f"ditto-agent-{first_id}.tar.gz",
            f"ditto-agent-{second_id}.tar.gz",
        }

    async def test_public_release_download_is_audited(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The one route that serves source to anyone still records the serve.

        There is no requester identity to record here -- that is the point of a
        public release -- so the row carries the peer address and the fact that
        the bytes went out at all. Audited only after the release gate passes,
        so an unauthenticated caller cannot drive row inserts by knocking.
        """
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        await _set_score_created_times(
            session_maker,
            agent_id=agent_id,
            created_at=[
                now - timedelta(hours=51),
                now - timedelta(hours=50),
                now - timedelta(hours=49),
            ],
        )
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=49),
        )
        _install_db(app, session_maker)
        storage = AsyncMock()
        storage.presigned_get_url.return_value = "https://objects.example/source"

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")

        assert response.status_code == 200
        async with session_maker() as s:
            rows = (await s.scalars(select(ArtifactFetchAudit))).all()
        assert len(rows) == 1
        assert str(rows[0].agent_id) == str(agent_id)
        assert rows[0].endpoint == "public.agent_artifact"
        assert rows[0].requester_kind == "public"
        # No identity exists on this route; the CHECK constraint requires the
        # column to be NULL rather than a misleading placeholder.
        assert rows[0].requester_id is None

    async def test_embargoed_request_writes_no_audit_row(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A refused fetch served no bytes, so it is not an artifact fetch."""
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        await _set_score_created_times(
            session_maker,
            agent_id=agent_id,
            created_at=[now, now, now],
        )
        await _crown(session_maker, agent_id=agent_id, first_crowned_at=now)
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")

        assert response.status_code == 425
        async with session_maker() as s:
            assert (
                await s.scalar(select(func.count()).select_from(ArtifactFetchAudit))
            ) == 0

    async def test_fourth_score_does_not_restart_the_quorum_embargo(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6, 0.7],
        )
        await _set_score_created_times(
            session_maker,
            agent_id=agent_id,
            created_at=[
                now - timedelta(hours=27),
                now - timedelta(hours=26),
                now - timedelta(hours=25),
                now - timedelta(minutes=5),
            ],
        )
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=49),
        )
        _install_db(app, session_maker)
        storage = AsyncMock()
        storage.presigned_get_url.return_value = "https://objects.example/source"

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
        assert response.status_code == 200

    async def test_shortened_setting_releases_existing_quorums_retroactively(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        await _set_score_created_times(
            session_maker,
            agent_id=agent_id,
            created_at=[
                now - timedelta(hours=8),
                now - timedelta(hours=7),
                now - timedelta(hours=6, minutes=1),
            ],
        )
        # King since just over the shortened 6-hour window ago.
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=6, minutes=1),
        )
        async with session_maker() as session, session.begin():
            # The migration chain seeds the operative default, so a new
            # revision must chain onto the current head -- parent_revision is
            # UNIQUE and the table is never empty in production.
            head = await session.scalar(
                select(func.max(ArtifactReleaseSettingsRevision.revision))
            )
            session.add(
                ArtifactReleaseSettingsRevision(
                    parent_revision=head or 0,
                    embargo_hours=6,
                    reason="Complete the staged privacy rollout",
                    actor="operator@example.com",
                )
            )
        _install_db(app, session_maker)
        storage = AsyncMock()
        storage.presigned_get_url.return_value = "https://objects.example/source"

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
        assert response.status_code == 200
        submission = (await client.get("/api/v1/public/submissions")).json()[
            "submissions"
        ][0]
        assert submission["artifact_release"]["embargo_hours"] == 6
        assert submission["artifact_release"]["status"] == "available"

    async def test_embargo_and_review_hold_fail_closed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        embargoed_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        held_id = await _seed_k3(
            session_maker,
            miner=_MINER_B,
            composites=[0.9, 0.9, 0.9],
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        await _set_score_created_times(
            session_maker,
            agent_id=embargoed_id,
            created_at=[
                now - timedelta(hours=3),
                now - timedelta(hours=2),
                now - timedelta(hours=1),
            ],
        )
        # King only an hour ago, so still inside the 48h window: embargoed.
        await _crown(
            session_maker,
            agent_id=embargoed_id,
            first_crowned_at=now - timedelta(hours=1),
        )
        await _set_score_created_times(
            session_maker,
            agent_id=held_id,
            created_at=[
                now - timedelta(hours=10),
                now - timedelta(hours=9),
                now - timedelta(hours=8),
            ],
        )
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        embargoed = await client.get(f"/api/v1/public/agent/{embargoed_id}/artifact")
        assert embargoed.status_code == 425
        assert "embargoed until" in embargoed.json()["message"]
        held = await client.get(f"/api/v1/public/agent/{held_id}/artifact")
        assert held.status_code == 404
        storage.presigned_get_url.assert_not_awaited()

        entries = (await client.get("/api/v1/public/activity")).json()["entries"]
        held_entry = next(entry for entry in entries if entry["agent_id"] == held_id)
        assert held_entry["artifact_release"]["status"] == "under_review"
        assert held_entry["artifact_release"]["download_available"] is False

    async def test_scores_from_different_versions_do_not_form_a_quorum(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5],
            status=AgentStatus.SCORED,
            details={"bench_version": _ERA},
        )
        async with session_maker() as session, session.begin():
            await upsert_score(
                session,
                agent_id=UUID(agent_id),
                validator_hotkey=_VALIDATOR_C,
                bench_version=_NEXT_ERA,
                run_id="run-next-era",
                seed=1,
                composite=0.6,
                tool_mean=0.6,
                memory_mean=0.6,
                median_ms=500,
                n=110,
                generated_at=datetime.now(UTC) - timedelta(hours=10),
                details={"bench_version": _NEXT_ERA},
            )
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        submissions = (await client.get("/api/v1/public/submissions")).json()
        assert submissions["submissions"][0]["artifact_release"]["status"] == (
            "awaiting_quorum"
        )
        # Never held the crown, so the source is king-only private (404), not a
        # timed embargo (425).
        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
        assert response.status_code == 404
        storage.presigned_get_url.assert_not_awaited()

    async def test_only_the_king_source_is_released(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        # A finalized submission that has never held the crown.
        commoner_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        # A submission that briefly reigned 49h ago and has since lost the crown.
        former_king_id = await _seed_k3(
            session_maker, miner=_MINER_B, composites=[0.7, 0.8, 0.9]
        )
        await _crown(
            session_maker,
            agent_id=former_king_id,
            first_crowned_at=now - timedelta(hours=49),
        )
        _install_db(app, session_maker)
        storage = AsyncMock()
        storage.presigned_get_url.return_value = "https://objects.example/source"

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        releases = {
            entry["agent_id"]: entry["artifact_release"]
            for entry in (await client.get("/api/v1/public/submissions")).json()[
                "submissions"
            ]
        }
        # The commoner's source is never released, even though it finalized 3/3.
        assert releases[commoner_id]["status"] == "unavailable"
        assert releases[commoner_id]["download_available"] is False
        assert releases[commoner_id]["crowned_at"] is None
        # The former king's brief reign still releases its source one window on.
        assert releases[former_king_id]["status"] == "available"
        assert releases[former_king_id]["download_available"] is True
        assert releases[former_king_id]["crowned_at"] is not None

        commoner = await client.get(f"/api/v1/public/agent/{commoner_id}/artifact")
        assert commoner.status_code == 404
        king = await client.get(f"/api/v1/public/agent/{former_king_id}/artifact")
        assert king.status_code == 200
        storage.presigned_get_url.assert_awaited_once()

    async def test_king_source_is_embargoed_until_the_window_elapses(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.7, 0.8, 0.9]
        )
        # Crowned only an hour ago: still inside the default 48h window.
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=1),
        )
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        release = (await client.get("/api/v1/public/submissions")).json()[
            "submissions"
        ][0]["artifact_release"]
        assert release["status"] == "embargoed"
        assert release["download_available"] is False
        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
        assert response.status_code == 425
        assert "embargoed until" in response.json()["message"]
        storage.presigned_get_url.assert_not_awaited()

    async def test_ever_king_awaiting_onchain_weight_stays_embargoed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.7, 0.8, 0.9]
        )
        # Touched the crown 49h ago, but the chain has not yet confirmed weights
        # were set on it: the window has NOT started, even though 48h elapsed.
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=49),
            weight_confirmed_at=None,
        )
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        release = (await client.get("/api/v1/public/submissions")).json()[
            "submissions"
        ][0]["artifact_release"]
        assert release["status"] == "embargoed"
        assert release["download_available"] is False
        assert release["available_at"] is None
        assert release["crowned_at"] is not None
        assert release["weight_confirmed_at"] is None
        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
        assert response.status_code == 425
        assert "on-chain" in response.json()["message"]
        storage.presigned_get_url.assert_not_awaited()


async def _set_never(maker: async_sessionmaker[AsyncSession]) -> None:
    """Append a `never` policy the way the admin board would.

    Chains onto the current head: `parent_revision` is UNIQUE and the
    migration chain seeds the operative default, so the table is never empty
    in production.
    """
    async with maker() as session, session.begin():
        head = await session.scalar(
            select(func.max(ArtifactReleaseSettingsRevision.revision))
        )
        session.add(
            ArtifactReleaseSettingsRevision(
                parent_revision=head or 0,
                disclosure="never",
                embargo_hours=48,
                reason="Subnet policy: submitted source is not published",
                actor="operator@example.com",
            )
        )


class TestNeverDiscloseReleasePolicy:
    """`disclosure = never`: the subnet publishes no source at all.

    The gate it overrides is narrow already -- king-only, chain-confirmed,
    embargoed -- so the cases worth pinning are the ones where every other term
    of that conjunction is satisfied and release still must not happen.
    """

    async def test_a_fully_released_king_is_withheld_under_the_never_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        first_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.7, 0.8, 0.9]
        )
        second_id = await _seed_k3(
            session_maker, miner=_MINER_B, composites=[0.4, 0.5, 0.6]
        )
        for agent_id in (first_id, second_id):
            await _crown(
                session_maker,
                agent_id=agent_id,
                first_crowned_at=now - timedelta(hours=72),
            )
        await _set_never(session_maker)
        _install_db(app, session_maker)
        storage = AsyncMock()
        storage.presigned_get_url.side_effect = lambda **kwargs: (
            f"https://objects.example/{kwargs['key']}"
        )

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        releases = {
            entry["agent_id"]: entry["artifact_release"]
            for entry in (await client.get("/api/v1/public/submissions")).json()[
                "submissions"
            ]
        }
        # Uniform: both submissions, identically, with no per-agent input to
        # the decision. There is nothing here for a miner to opt into or out
        # of, and so nothing to game.
        assert {release["status"] for release in releases.values()} == {"withheld"}
        assert {release["disclosure"] for release in releases.values()} == {"never"}
        assert all(
            release["download_available"] is False for release in releases.values()
        )
        assert all(release["available_at"] is None for release in releases.values())

        for agent_id in (first_id, second_id):
            refused = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
            # 403, not 425: 425 means "too early" and invites a retry loop
            # that would never terminate.
            assert refused.status_code == 403
            assert "withholds all submitted source" in refused.json()["message"]

        # The assertion that makes this a privacy policy rather than a label:
        # no presigned URL was minted for anything.
        storage.presigned_get_url.assert_not_awaited()

    async def test_withholding_reaches_every_public_release_surface(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Gating one endpoint would be a false promise.

        `artifact_release` is projected onto several public responses. A reader
        who consults the leaderboard rather than `/submissions` must get the
        same answer, or the console renders a download affordance the download
        route then refuses.
        """
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.7, 0.8, 0.9]
        )
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=72),
        )
        await _set_never(session_maker)
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        scores = (await client.get(f"/api/v1/public/agent/{agent_id}/scores")).json()[
            "artifact_release"
        ]
        assert scores["status"] == "withheld"
        assert scores["download_available"] is False

        entry = next(
            entry
            for entry in (await client.get("/api/v1/public/leaderboard")).json()[
                "entries"
            ]
            if entry["agent_id"] == agent_id
        )
        assert entry["artifact_release"]["status"] == "withheld"
        assert entry["artifact_release"]["download_available"] is False

        storage.presigned_get_url.assert_not_awaited()

    async def test_the_rest_of_the_public_record_is_unchanged(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Withholding source withholds source. It is not a hidden retirement.

        v7 is live and driving validator weights; a release-visibility setting
        that also removed submissions from the ledger would be a scoring change
        wearing a privacy label. Scores, the dataset pin and rank stay exactly
        as public as they were.
        """
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.7, 0.8, 0.9]
        )
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=72),
        )
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        before = (await client.get(f"/api/v1/public/agent/{agent_id}/scores")).json()
        await _set_never(session_maker)
        after = (await client.get(f"/api/v1/public/agent/{agent_id}/scores")).json()

        assert after["artifact_release"] != before["artifact_release"]
        # `generated_at` is the response timestamp and moves on every call.
        volatile = {"artifact_release", "generated_at"}
        assert {k: v for k, v in after.items() if k not in volatile} == {
            k: v for k, v in before.items() if k not in volatile
        }

    async def test_a_year_long_window_is_embargoed_not_withheld(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A year and `never` are different answers, and stay distinguishable.

        Under a long window the source is still going to be published, and the
        response still carries the instant. Collapsing the two would erase the
        one property that separates option 3 from option 4 -- that external
        verification is delayed rather than abolished.
        """
        now = datetime.now(UTC)
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.7, 0.8, 0.9]
        )
        await _crown(
            session_maker,
            agent_id=agent_id,
            first_crowned_at=now - timedelta(hours=72),
        )
        async with session_maker() as session, session.begin():
            head = await session.scalar(
                select(func.max(ArtifactReleaseSettingsRevision.revision))
            )
            session.add(
                ArtifactReleaseSettingsRevision(
                    parent_revision=head or 0,
                    disclosure="public",
                    embargo_hours=8760,
                    reason="Subnet policy: one-year disclosure window",
                    actor="operator@example.com",
                )
            )
        _install_db(app, session_maker)
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage

        release = (await client.get("/api/v1/public/submissions")).json()[
            "submissions"
        ][0]["artifact_release"]
        assert release["status"] == "embargoed"
        assert release["disclosure"] == "public"
        assert release["embargo_hours"] == 8760
        # The unlock instant is published: delayed disclosure, not withheld.
        assert release["available_at"] is not None

        response = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
        assert response.status_code == 425
        storage.presigned_get_url.assert_not_awaited()


async def _seed_audit(maker: async_sessionmaker[AsyncSession], *, n: int) -> None:
    """Append ``n`` chained score entries to the audit log."""
    async with maker() as s, s.begin():
        for i in range(n):
            await append_audit_entry(
                s,
                agent_id=uuid4(),
                validator_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                event=EVENT_SCORE,
                payload={"run_id": f"run_{i}", "composite": 0.5, "seed": 42},
                recorded_at=datetime(2026, 6, 8, 12, i, 0, tzinfo=UTC),
            )


class _FakeRevealGenerator:
    """Stands in for the data-pipeline generate service on the reveal path."""

    def __init__(
        self,
        *,
        artifact: dict | None = None,
        sha: str = "cd" * 32,
        fail: bool = False,
    ) -> None:
        self._artifact = artifact if artifact is not None else {"bench_version": _ERA}
        self._sha = sha
        self._fail = fail
        self.calls = 0
        self.bench_versions: list[int] = []

    async def fetch_dataset(
        self, seed: int, run_size: str, bench_version: int
    ) -> tuple[dict, str]:
        # ``bench_version`` is required, not defaulted. The reveal endpoint used
        # to omit it and take the old default of 2, which served the v2 dataset
        # for every finalized agent regardless of the era it ran. Recording it
        # here is what lets a test assert the endpoint asked for the right one.
        self.calls += 1
        self.bench_versions.append(bench_version)
        if self._fail:
            raise DataPipelineError("generate service down")
        return {**self._artifact, "seed": seed, "run_size": run_size}, self._sha


def _install_generator(app: FastAPI, generator: object) -> None:
    async def _gen() -> object:
        return generator

    app.dependency_overrides[get_dataset_generator] = _gen


class TestPublicDatasetReveal:
    async def test_reveals_full_labeled_dataset_for_finalized_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        _install_db(app, session_maker)
        # The generator returns a dataset whose sha matches the pinned "cd"*32.
        artifact = {"bench_version": _ERA, "tool_cases": [{"expected_tools": ["x"]}]}
        gen = _FakeRevealGenerator(artifact=artifact, sha="cd" * 32)
        _install_generator(app, gen)

        resp = await client.get(f"/api/v1/public/agent/{agent_id}/dataset")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == agent_id
        assert body["seed"] == 987654321
        assert body["run_size"] == "full"
        assert body["dataset_sha256"] == "cd" * 32
        assert body["bench_version"] == _ERA
        # The dataset asked for is the one this agent actually ran. This is the
        # regression: the endpoint omitted `bench_version` entirely and took the
        # old default of 2, so every finalized agent was revealed the v2
        # dataset. It never errored -- a v2 dataset is well-formed -- so only
        # asserting the era the generator was ASKED for catches it coming back.
        assert gen.bench_versions == [_ERA]
        # The FULL labeled artifact (answer keys included) is served.
        assert body["artifact"]["tool_cases"][0]["expected_tools"] == ["x"]
        assert gen.calls == 1

    async def test_404_for_unfinalized_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4],
            status=AgentStatus.EVALUATING,
        )
        _install_db(app, session_maker)
        _install_generator(app, _FakeRevealGenerator())
        resp = await client.get(f"/api/v1/public/agent/{agent_id}/dataset")
        assert resp.status_code == 404

    async def test_502_on_generator_hash_drift(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        _install_db(app, session_maker)
        # Generator returns a DIFFERENT sha than the pinned "cd"*32.
        _install_generator(app, _FakeRevealGenerator(sha="ab" * 32))
        resp = await client.get(f"/api/v1/public/agent/{agent_id}/dataset")
        assert resp.status_code == 502

    async def test_503_when_generator_unavailable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_k3(
            session_maker, miner=_MINER_A, composites=[0.4, 0.5, 0.6]
        )
        _install_db(app, session_maker)
        _install_generator(app, _FakeRevealGenerator(fail=True))
        resp = await client.get(f"/api/v1/public/agent/{agent_id}/dataset")
        assert resp.status_code == 503


class TestPublicBenchCorpus:
    async def test_retired_version_serves_full_answer_keys(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A run scored under a retired era, with a newer one active. Its full
        # per-case answer keys are released verbatim. The era is retired in the
        # ledger's sense too now -- the floor refuses new rows on it -- so the
        # grandfathered rows have to be written the way production holds them.
        details = {
            "bench_version": _PREV_ERA,
            "per_case": [
                {
                    "category": "web_search",
                    "score": 0.6,
                    "expected": ["search_web"],
                    "called": ["search_web"],
                    "case_id": "web_search-1-0001",
                }
            ],
        }
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.4, 0.5, 0.6],
                details=details,
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        resp = await client.get(f"/api/v1/public/bench/{_PREV_ERA}/corpus")
        assert resp.status_code == 200
        body = resp.json()
        assert body["bench_version"] == _PREV_ERA
        assert body["total"] == 3  # three validator rows
        entry = body["entries"][0]
        # The FULL answer key is present (retired = safe).
        assert entry["per_case"][0]["expected"] == ["search_web"]
        assert entry["per_case"][0]["case_id"] == "web_search-1-0001"

    async def test_live_version_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The current (live) version: its answer keys must never be released.
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6],
            details={
                "bench_version": CURRENT_BENCH_VERSION,
                "per_case": [{"expected": ["x"]}],
            },
        )
        _install_db(app, session_maker)
        resp = await client.get(f"/api/v1/public/bench/{CURRENT_BENCH_VERSION}/corpus")
        assert resp.status_code == 409
        # ...and the live answer key is not in the refusal body.
        assert '"expected"' not in resp.text

    async def test_active_corpus_remains_private_before_the_next_activation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The era still in force keeps its answer keys until a newer one lands.

        Retirement is relative to the *active* version, not to whatever the
        newest shipped contract happens to be, so the era being scored right
        now is refused even though runs exist for it.
        """
        await _seed_k3(
            session_maker,
            miner=_MINER_A,
            composites=[0.4, 0.5, 0.6],
            details={
                "bench_version": _ERA,
                "per_case": [{"expected": ["still-live"]}],
            },
        )
        await _activate_era(session_maker)
        _install_db(app, session_maker)

        response = await client.get(f"/api/v1/public/bench/{_ERA}/corpus")

        assert response.status_code == 409
        assert '"expected"' not in response.text

    async def test_retired_version_paginates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            await _seed_k3(
                session_maker,
                miner=_MINER_A,
                composites=[0.4, 0.5, 0.6],
                details={"bench_version": _PREV_ERA, "per_case": []},
            )
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        page = (
            await client.get(f"/api/v1/public/bench/{_PREV_ERA}/corpus?limit=2")
        ).json()
        assert page["count"] == 2
        assert page["total"] == 3
        page2 = (
            await client.get(
                f"/api/v1/public/bench/{_PREV_ERA}/corpus?limit=2&offset=2"
            )
        ).json()
        assert page2["count"] == 1

    async def test_retired_version_with_no_runs_is_empty(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _activate_era(session_maker)
        _install_db(app, session_maker)
        body = (await client.get(f"/api/v1/public/bench/{_PREV_ERA}/corpus")).json()
        assert body["total"] == 0
        assert body["entries"] == []


class TestPublicAudit:
    async def test_feed_returns_chained_entries(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_audit(session_maker, n=3)
        _install_db(app, session_maker)

        resp = await client.get("/api/v1/public/audit")
        assert resp.status_code == 200
        assert (
            resp.headers["Cache-Control"]
            == "public, max-age=30, stale-while-revalidate=120"
        )
        body = resp.json()
        assert body["count"] == 3
        assert body["genesis_hash"] == GENESIS_HASH
        entries = body["entries"]
        # Oldest first, contiguous seqs, and each links to the prior entry_hash.
        assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)
        assert entries[0]["prev_hash"] == GENESIS_HASH
        for prev, cur in zip(entries, entries[1:], strict=False):
            assert cur["prev_hash"] == prev["entry_hash"]
        assert body["head_hash"] == entries[-1]["entry_hash"]
        # The signed-tuple payload is present; no per-case answer key ever is.
        assert entries[0]["payload"]["run_id"] == "run_0"
        assert '"per_case"' not in resp.text

    async def test_feed_pages_by_since_seq(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_audit(session_maker, n=5)
        _install_db(app, session_maker)

        first = (await client.get("/api/v1/public/audit?limit=2")).json()
        assert first["count"] == 2
        last_seq = first["entries"][-1]["seq"]
        nxt = (await client.get(f"/api/v1/public/audit?since_seq={last_seq}")).json()
        assert nxt["count"] == 3
        assert nxt["entries"][0]["seq"] > last_seq
        # The page still links onto the first page's head.
        assert nxt["entries"][0]["prev_hash"] == first["head_hash"]

    async def test_feed_empty(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        body = (await client.get("/api/v1/public/audit")).json()
        assert body["count"] == 0
        assert body["entries"] == []
        assert body["head_hash"] is None
        assert body["genesis_hash"] == GENESIS_HASH


class TestBenchConfig:
    """GET /public/bench/config exposes the frozen-model + grading setup."""

    async def test_config_shape_and_defaults(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch,
    ) -> None:
        _install_db(app, session_maker)
        monkeypatch.delenv("STORAGE_PUBLIC_BUCKET", raising=False)
        resp = await client.get("/api/v1/public/bench/config")
        assert resp.status_code == 200
        assert "max-age=300" in resp.headers["Cache-Control"]
        body = resp.json()
        # Nothing has activated in this database, so the ledger's honest answer
        # is the floor. Which generation that is happens to be incidental to
        # everything else asserted below.
        assert body["bench_version"] == _ERA
        h = body["harness"]
        assert h["locked"] is True
        # The harness block is derived from the era being reported, so it moves
        # with it: from v7 on the canonical model is the proxy-routed one and
        # reasoning effort is pinned rather than absent.
        assert h["canonical_id"] == "openai/gpt-oss-20b"
        assert h["serving"] == "OpenRouter dynamic provider route"
        assert h["thinking"] is True
        assert h["reasoning_effort"] == "medium"
        assert body["grading"]["judge_free"] is True
        assert "dittobench-datagen" in body["grading"]["grader"]
        assert "dataset_sha256" in body["dataset"]["reproduce"]
        assert body["public_mirror_url_template"] is None
        assert body["public_transcript_url_template"] is None
        assert body["public_transcript_telemetry_url_template"] == (
            "/api/v1/public/bench/transcript/{sha256}/telemetry"
        )
        assert body["ledger_path"] == "/api/v1/scoring/scores"

    async def test_open_v7_rollout_keeps_active_v6_harness_authoritative(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch,
    ) -> None:
        _install_db(app, session_maker)
        monkeypatch.delenv("BENCH_HARNESS_MODEL_ID", raising=False)
        monkeypatch.delenv("BENCH_HARNESS_SERVING", raising=False)
        # The active era has to be the RETIRED one this test is named after, or
        # there is nothing to assert: with no activation on record the ledger
        # answers the floor, which is the rollout's own target, and
        # "active != desired" -- the whole point -- collapses to 7 == 7.
        #
        # v6 held authority exactly this way in production, through an activated
        # rollout that now sits beneath the floor. Grandfathering it is
        # reproducing that row, not inventing one.
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
            floor_session.begin(),
        ):
            await grandfather_active_era(
                floor_session,
                version=_PREV_ERA,
                now=datetime(2026, 6, 1, tzinfo=UTC),
                from_version=DEFAULT_BENCH_VERSION,
            )
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_PREV_ERA,
                    desired_version=_ERA,
                    status="collecting",
                    cohort_size=5,
                    created_at=datetime.now(UTC),
                )
            )

        body = (await client.get("/api/v1/public/bench/config")).json()

        assert body["bench_version"] == _PREV_ERA
        assert body["desired_bench_version"] == _ERA
        assert body["harness"]["canonical_id"] == "qwen/qwen3-32b"
        assert body["harness"]["serving"] == "Qwen/Qwen3-32B-TEE"
        assert body["harness"]["thinking"] is False
        assert body["harness"]["reasoning_effort"] is None

    async def test_mirror_template_from_env(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch,
    ) -> None:
        _install_db(app, session_maker)
        monkeypatch.setenv("STORAGE_PUBLIC_BUCKET", "ditto-platform-public-dev")
        body = (await client.get("/api/v1/public/bench/config")).json()
        assert body["public_mirror_url_template"] == (
            "https://storage.googleapis.com/ditto-platform-public-dev/scored/{agent_id}.json"
        )
        assert body["public_transcript_url_template"] == (
            "https://storage.googleapis.com/ditto-platform-public-dev/transcripts/{sha256}.json"
        )
        assert body["public_transcript_telemetry_url_template"] == (
            "/api/v1/public/bench/transcript/{sha256}/telemetry"
        )

    async def test_transcript_telemetry_is_verified_allowlisted_and_immutable(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        body = json.dumps(
            {
                "execution": {"cases": 1, "succeeded": 1, "max_duration_ms": 25},
                "model_relay": {"requests": 2, "successes": 1},
                "cases": [
                    {
                        "prompt": "private question",
                        "response": "private answer",
                        "execution": {
                            "total_duration_ms": 25,
                            "terminal_outcome": "success",
                            "attempts": [
                                {
                                    "attempt": 1,
                                    "duration_ms": 25,
                                    "outcome": "success",
                                    "http_status": 200,
                                    "error": "private raw error",
                                }
                            ],
                        },
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(body).hexdigest()
        storage = AsyncMock()
        storage.get_object.return_value = body

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage
        response = await client.get(
            f"/api/v1/public/bench/transcript/{digest}/telemetry"
        )

        assert response.status_code == 200
        assert response.json() == {
            "source_sha256": digest,
            "execution": {
                "cases": 1,
                "succeeded": 1,
                "timed_out": 0,
                "cancelled": 0,
                "retried": 0,
                "total_attempts": 0,
                "median_duration_ms": None,
                "p95_duration_ms": None,
                "max_duration_ms": 25,
            },
            "model_relay": {
                "requests": 2,
                "successes": 1,
                "infrastructure_failures": 0,
                "caller_cancellations": 0,
                "upstream_attempts": 0,
                "retries": 0,
            },
            "cases": [
                {
                    "position": 1,
                    "total_duration_ms": 25,
                    "terminal_outcome": "success",
                    "timed_out": False,
                    "cancelled": False,
                    "attempts": [
                        {
                            "attempt": 1,
                            "duration_ms": 25,
                            "outcome": "success",
                            "http_status": 200,
                        }
                    ],
                }
            ],
        }
        assert "private" not in response.text
        assert "immutable" in response.headers["cache-control"]
        storage.get_object.assert_awaited_once_with(
            key=f"transcripts/{digest}.json", max_bytes=32 << 20
        )

    async def test_transcript_rejects_bad_address_and_stored_digest(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        storage = AsyncMock()

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage
        assert (
            await client.get("/api/v1/public/bench/transcript/not-a-digest/telemetry")
        ).status_code == 404
        storage.get_object.assert_not_awaited()

        expected = "0" * 64
        storage.get_object.return_value = b"{}"
        response = await client.get(
            f"/api/v1/public/bench/transcript/{expected}/telemetry"
        )
        assert response.status_code == 502

    async def test_transcript_missing_is_not_publicly_distinguishable(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        storage = AsyncMock()
        storage.get_object.side_effect = ObjectDownloadFailedError("missing")

        async def _storage():
            return storage

        app.dependency_overrides[get_storage_client] = _storage
        response = await client.get(
            "/api/v1/public/bench/transcript/" + "a" * 64 + "/telemetry"
        )
        assert response.status_code == 404


def test_bench_glossary_explains_every_v5_category_and_metric() -> None:
    from ditto.api_models import bench_glossary as bg

    cats = {c["key"]: c for c in bg.category_entries()}
    # The v5 families the composite quality gate hinges on must be documented.
    for key in (
        "conversational-chitchat",
        "conversational-declarative",
        "declarative-write",
        "declarative-write-read",
        "declarative-behavior",
        "multi-hop-relational",
        "temporal-depth",
        "canary",
        # bench_version 6 complexity classes
        "injection-stored-instruction",
        "stored-instruction-benign",
        "multi-query-recall",
        "nonverbatim-computed",
        "passive-consolidation",
    ):
        assert key in cats, f"undocumented category: {key}"
    # Every entry is complete and public-safe (a purpose, a known kind, no blanks),
    # and carries a concrete illustrative example so the glossary shows what each
    # case actually looks like, not just what it probes.
    kinds = {"memory", "conversational", "tool", "multi_step", "integrity"}
    for c in cats.values():
        assert c["label"] and c["purpose"]
        assert c["kind"] in kinds
        assert c["example"], f"category missing example: {c['key']}"
    # The metrics / quality factors that pull the composite below the halves.
    metrics = {m["key"] for m in bg.metric_entries()}
    # bench_version changelog is present, newest first, complete per version.
    versions = bg.version_entries()
    assert [v["version"] for v in versions] == [7, 6, 5, 4, 3, 2]
    for v in versions:
        assert v["title"] and v["summary"] and v["epoch"]

    v7 = versions[0]
    assert v7["title"] == "GPT-OSS inference contract"
    assert "openai/gpt-oss-20b" in v7["summary"]
    assert "medium" in v7["summary"]
    assert any("Same generated questions" in item for item in v7["highlights"])

    for key in (
        "composite",
        "conversational_sanity",
        "metamorphic_consistency",
        "tool_efficiency",
        "token_efficiency",
    ):
        assert key in metrics, f"undocumented metric: {key}"


class TestBenchmarkStallDetection:
    """``_benchmark_stalled`` must separate a stuck run from a stuck stream.

    The operator complaint this answers is "I cannot tell whether the bench is
    wedged or the reporting is". Those are two different faults with two
    different owners, and they are computed from two different signals:
    ``stalled`` reads the run's own reported progress against elapsed time, while
    liveness (``online`` / ``heartbeat_stale``) reads ``seen_at``. A run that is
    frozen but reporting must read stalled-and-online; a validator that has gone
    quiet must not be reported as a stalled run.
    """

    _START = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    def test_an_early_stage_stalls_on_wall_clock_alone(self) -> None:
        """Pre-run stages have no count to judge, so the clock is all there is."""
        assert not public_endpoint._benchmark_stalled(
            "generating_dataset", self._START, self._START + timedelta(minutes=14)
        )
        assert public_endpoint._benchmark_stalled(
            "generating_dataset", self._START, self._START + timedelta(minutes=16)
        )

    def test_a_healthy_running_benchmark_is_never_called_stalled(self) -> None:
        """A v7 run near its real ~4s/check pace must stay clean throughout.

        This is the regression that matters most: mislabelling a working run as
        stuck is worse than the missing signal it replaces, because it trains the
        operator to ignore the badge.
        """
        for completed in range(0, 282, 7):
            elapsed = timedelta(seconds=4 * completed)
            assert not public_endpoint._benchmark_stalled(
                "running_benchmark",
                self._START,
                self._START + elapsed,
                completed=completed,
            )

    def test_a_frozen_running_benchmark_is_flagged(self) -> None:
        """A count stuck at 3/281 cannot explain three quarters of an hour."""
        assert public_endpoint._benchmark_stalled(
            "running_benchmark",
            self._START,
            self._START + timedelta(minutes=45),
            completed=3,
        )

    def test_the_startup_grace_covers_a_slow_first_check(self) -> None:
        """Zero completed checks is normal for a while; it is not yet a stall."""
        assert not public_endpoint._benchmark_stalled(
            "running_benchmark",
            self._START,
            self._START + timedelta(minutes=14),
            completed=0,
        )
        assert public_endpoint._benchmark_stalled(
            "running_benchmark",
            self._START,
            self._START + timedelta(minutes=16),
            completed=0,
        )

    def test_an_unreported_count_does_not_invent_a_stall(self) -> None:
        """Missing telemetry is not evidence of a wedged run.

        A validator that reports the stage but omits counts (an older protocol, or
        a poll that degraded to unknown) must fall back to the plain grace window
        rather than being treated as frozen at zero.
        """
        for elapsed in (timedelta(minutes=14), timedelta(hours=1), timedelta(hours=9)):
            assert not public_endpoint._benchmark_stalled(
                "running_benchmark",
                self._START,
                self._START + elapsed,
                completed=None,
            )

    def test_a_terminal_stage_is_never_stalled(self) -> None:
        """Finalizing and submitting are bounded by the validator, not by us."""
        for stage in ("finalizing", "submitting_result", "failed_retrying"):
            assert not public_endpoint._benchmark_stalled(
                stage,  # type: ignore[arg-type]
                self._START,
                self._START + timedelta(hours=6),
                completed=281,
            )

    def test_stall_is_independent_of_reporting_liveness(self) -> None:
        """The two signals are orthogonal, and must be computed independently.

        ``_benchmark_stalled`` is a pure function of the run's own progress and
        never consults ``seen_at``; a stalled run therefore still reads online so
        long as it keeps heartbeating. Asserting both here pins the separation
        that makes the badge trustworthy.
        """
        now = self._START + timedelta(minutes=45)
        assert public_endpoint._benchmark_stalled(
            "running_benchmark", self._START, now, completed=3
        )
        online, _availability, _health = _fleet_classification(
            state="running_benchmark", seen_at=now, now=now, metrics=None
        )
        assert online is True


class TestPublicProgressResolution:
    """``percent`` is exact, and the allowlist it lives in stays closed.

    The 5% quantizer was removed because it withheld nothing: ``completed_checks``
    and ``total_checks`` are published exactly on the same model, so the ratio was
    always derivable. These tests pin both halves of that argument — the
    resolution is now exact, *and* nothing per-case rode in alongside it.
    """

    def test_operations_keeps_an_active_retest_beyond_terminal_history(self) -> None:
        """A finalized top-five row remains visible while its retest runs."""
        active_id = uuid4()
        projected = [
            (SimpleNamespace(agent=SimpleNamespace(agent_id=uuid4())), "scored")
            for _ in range(51)
        ]
        active_row = (
            SimpleNamespace(agent=SimpleNamespace(agent_id=active_id)),
            "live",
        )
        rows = public_endpoint._operations_activity_rows(
            [*projected, active_row],
            board_statuses={"evaluating"},
            board_active_agent_ids={active_id},
            terminal_history_limit=50,
        )

        assert active_row in rows
        assert len(rows) == 51
        assert rows.count(active_row) == 1

    def test_operations_keeps_conditional_integrity_review_visible(self) -> None:
        review_row = (
            SimpleNamespace(agent=SimpleNamespace(agent_id=uuid4())),
            "under_review",
        )

        rows = public_endpoint._operations_activity_rows(
            [review_row],
            board_statuses={"evaluating", "under_review"},
            board_active_agent_ids=set(),
            terminal_history_limit=50,
        )

        assert rows == [review_row]

    @staticmethod
    def _progress(
        stage: str, completed: int | None, total: int | None
    ) -> PublicBenchmarkProgress:
        now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        work = SimpleNamespace(
            agent=SimpleNamespace(agent_id=uuid4(), name="lihai"),
            ticket=SimpleNamespace(slot_id="slot-0", bench_version=7, issued_at=now),
            progress=SimpleNamespace(stage=stage, completed=completed, total=total),
        )
        return public_endpoint._public_benchmark_progress(work, now)  # type: ignore[arg-type]

    def test_percent_is_exact_not_bucketed(self) -> None:
        """Consecutive checks now move the bar; they used to be invisible.

        On a 281-check v7 run a 5% bucket was ~14 checks, so fourteen consecutive
        completions rendered as no change at all. That is the "is it stuck?"
        feeling the operations page was producing.
        """
        assert self._progress("running_benchmark", 53, 281).percent == 18
        assert self._progress("running_benchmark", 54, 281).percent == 19
        assert self._progress("running_benchmark", 140, 281).percent == 49

    def test_a_full_bar_means_finished(self) -> None:
        """281/281 is held at 99% until the run leaves the scoring stages."""
        assert self._progress("running_benchmark", 281, 281).percent == 99
        assert self._progress("finalizing", 281, 281).percent == 100
        assert self._progress("submitting_result", 281, 281).percent == 100

    def test_counts_and_percent_agree(self) -> None:
        """The published percent must be reproducible from the published counts."""
        rendered = self._progress("running_benchmark", 53, 281)
        assert rendered.completed_checks == 53
        assert rendered.total_checks == 281
        assert rendered.percent == rendered.completed_checks * 100 // (
            rendered.total_checks
        )

    def test_an_unreported_count_publishes_no_percent(self) -> None:
        assert self._progress("preparing", None, None).percent is None

    def test_no_per_case_field_rides_along(self) -> None:
        """The in-flight allowlist stays closed.

        Per-case identity, question text, verdicts, seeds and timings are all
        excluded from live progress by construction. This is not stylistic: the
        dataset seed is drawn after screening and published only post-hoc
        (anti-overfit), and the run's canary case is identifiable from its
        category alone, so a live per-case feed would defeat both. Any new field
        here must be argued on that basis first.
        """
        rendered = self._progress("running_benchmark", 53, 281)
        published = rendered.model_dump(mode="json")
        assert set(published) == {
            "agent_id",
            "slot_id",
            "agent_name",
            "bench_version",
            "started_at",
            "stage",
            "completed_checks",
            "total_checks",
            "percent",
            "stalled",
        }
        forbidden = {
            "case_id",
            "case_category",
            "category",
            "prompt",
            "question",
            "expected",
            "called",
            "canary",
            "seed",
            "dataset_sha256",
            "per_case",
            "partial",
            "verdict",
            "correct",
            "notes",
            "latency_ms",
            "run_token",
        }
        assert forbidden.isdisjoint(published)
