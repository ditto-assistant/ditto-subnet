"""Fork must beat its base (SN-302): detection, the paired duel, and the gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ditto.api_models.emission_eligibility import EmissionEligibilitySettings
from ditto.api_server.distinctive_content import KIND_TEXT, item_key
from ditto.api_server.emission_eligibility import ResolvedEligibilityPolicy, classify
from ditto.api_server.fork_gate import ForkDuel, detect_released_base, fork_duel
from ditto.api_server.koth import KothEntry
from ditto.api_server.unreleased_copy import IndexedAgent
from ditto.db.queries.emission_eligibility import AgentReviewPosture

_T0 = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
_BASE = UUID(int=1)
_FORK = UUID(int=2)
_OTHER = UUID(int=3)
# 60 distinctive text items: well above the 40-point fork threshold.
_BASE_KEYS = frozenset(item_key(KIND_TEXT, f"dan0l prompt line {i}") for i in range(60))


def _agent(
    agent_id: UUID,
    owner: str,
    *,
    minutes: int,
    keys: frozenset[int],
    released_minutes: int | None = None,
) -> IndexedAgent:
    return IndexedAgent(
        agent_id=agent_id,
        owner=owner,
        uploaded_at=_T0 + timedelta(minutes=minutes),
        released_at=(
            _T0 + timedelta(minutes=released_minutes)
            if released_minutes is not None
            else None
        ),
        keys=keys,
    )


def test_a_copy_of_another_owners_released_agent_is_a_fork() -> None:
    """The beast case: uploaded 8 minutes after the base was released."""
    base = _agent(_BASE, "dan0l", minutes=0, keys=_BASE_KEYS, released_minutes=17)
    fork = _agent(
        _FORK,
        "beast",
        minutes=25,
        keys=_BASE_KEYS | {item_key(KIND_TEXT, "one new sentence")},
    )

    found = detect_released_base(fork, [base])

    assert found is not None
    assert found.base_agent_id == _BASE
    assert found.basis == "distinctive"
    assert found.shared_count == 60


@pytest.mark.parametrize(
    ("owner", "released_minutes", "court"),
    [
        ("dan0l", 17, ()),  # same-owner successor: exempt
        ("beast", None, ()),  # base never released: the unreleased-copy signal's job
        ("beast", 30, ()),  # released only after the upload
    ],
)
def test_successors_and_unreleased_bases_are_not_forks(
    owner: str, released_minutes: int | None, court: tuple[UUID, ...]
) -> None:
    base = _agent(
        _BASE, "dan0l", minutes=0, keys=_BASE_KEYS, released_minutes=released_minutes
    )
    fork = _agent(_FORK, owner, minutes=25, keys=_BASE_KEYS)

    assert detect_released_base(fork, [base], court_bases=court) is None


def test_the_base_with_the_most_shared_content_wins_and_court_bases_count() -> None:
    small = frozenset(list(_BASE_KEYS)[:5])
    base = _agent(_BASE, "dan0l", minutes=0, keys=_BASE_KEYS, released_minutes=17)
    other = _agent(_OTHER, "horizon", minutes=1, keys=small, released_minutes=2)
    fork = _agent(_FORK, "beast", minutes=25, keys=_BASE_KEYS)

    best = detect_released_base(fork, [other, base])
    assert best is not None and best.base_agent_id == _BASE
    # A thin overlap alone is not a fork, but a copy-court released-base clear is.
    thin = _agent(_FORK, "beast", minutes=25, keys=small)
    assert detect_released_base(thin, [other]) is None
    cited = detect_released_base(thin, [other], court_bases={_OTHER})
    assert cited is not None and cited.basis == "court"


def _seeded(agent_id: UUID, scores: dict[int, float]) -> KothEntry:
    ordered = sorted(scores.items())
    return KothEntry(
        miner_hotkey=str(agent_id),
        agent_id=agent_id,
        composite=0.9,
        first_seen=_T0,
        raw_rank=1,
        bench_version=14,
        confirmation_seeds=tuple(seed for seed, _ in ordered),
        confirmation_composites=tuple(value for _, value in ordered),
    )


def test_a_fork_must_win_the_paired_duel_on_three_shared_seeds() -> None:
    base = _seeded(_BASE, {1: 0.90, 2: 0.91, 3: 0.89, 4: 0.90})
    ahead = {1: 0.95, 2: 0.96, 3: 0.945}

    assert (
        fork_duel(_seeded(_FORK, ahead), base, base_agent_id=_BASE).state
        == "beats_base"
    )
    two = fork_duel(_seeded(_FORK, {1: 0.95, 2: 0.96}), base, base_agent_id=_BASE)
    assert (two.state, two.shared_seeds) == ("unproven", 2)
    tied = fork_duel(
        _seeded(_FORK, {1: 0.90, 2: 0.91, 3: 0.89}), base, base_agent_id=_BASE
    )
    assert tied.state == "unproven" and tied.lead_needed and tied.lead_needed > 0
    assert fork_duel(_seeded(_FORK, ahead), None, base_agent_id=_BASE).shared_seeds == 0


@pytest.mark.parametrize(
    ("mode", "reward_eligible"), [("shadow", True), ("enforce", False)]
)
def test_only_enforce_withholds_an_unproven_fork(
    mode: str, reward_eligible: bool
) -> None:
    policy = ResolvedEligibilityPolicy(
        settings=EmissionEligibilitySettings(fork_must_beat_base=mode)  # type: ignore[arg-type]
    )
    duel = ForkDuel(
        base_agent_id=_BASE,
        shared_seeds=0,
        required_seeds=3,
        paired_lead=None,
        required_lead=None,
        state="unproven",
    )

    record = classify(
        agent_id=_FORK,
        artifact_sha256="ab" * 32,
        bench_version=14,
        posture=AgentReviewPosture(agent_id=_FORK),
        policy=policy,
        now=_T0,
        fork=duel,
    )

    # The review gate stays off: only the fork verdict is reported.
    assert record.enforcement == "off"
    assert record.state == "fork_unproven"
    assert record.posture_satisfied is False
    assert record.reward_eligible is reward_eligible
    assert record.fork_base_agent_id == _BASE
