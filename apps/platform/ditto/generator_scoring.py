"""Python port of the generator competition's discrimination score.

The reference implementations are ``challenge.ScoreCases`` and
``challenge.ScoreRun`` in ``research/dittobench-datagen/challenge/score.go``;
the formula and its
rationale are in ``research/dittobench-datagen/docs/generator-competition.md``.
``ditto/tests/test_generator_scoring.py`` replays the Go golden vectors
(``challenge/testdata/score_vectors.json`` and ``run_vectors.json``) and
requires every field to match,
so the two implementations cannot drift.

Operation order matters for bit-identical floats: panel members are visited in
``agent_id`` order and cases in pack order, exactly as the Go code does.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

CASE_DISCRIMINATING = "discriminating"
CASE_TRIVIAL = "trivial"
CASE_UNSOLVED = "unsolved"
CASE_DUPLICATE = "duplicate"
CASE_INSUFFICIENT_PANEL = "insufficient_panel"

PACK_SCORED = "scored"
PACK_INSUFFICIENT_PANEL = "insufficient_panel"

MIN_CASES = 8
MAX_CASES = 64


class ScoreInputError(ValueError):
    """The inputs cannot be scored (bad params, duplicate agents, bad scores)."""


@dataclass(frozen=True)
class ScoreParams:
    min_panel: int = 3
    min_solver_owners: int = 2
    solve_threshold: float = 0.5
    champion_bonus: float = 0.5
    target_cases: int = 32

    def validate(self) -> None:
        if not 2 <= self.min_panel <= 32:
            raise ScoreInputError("min_panel must be in 2..32")
        if not 1 <= self.min_solver_owners <= self.min_panel:
            raise ScoreInputError("min_solver_owners must be in 1..min_panel")
        if not 0 < self.solve_threshold <= 1:
            raise ScoreInputError("solve_threshold must be in (0, 1]")
        if not 0 <= self.champion_bonus <= 2:
            raise ScoreInputError("champion_bonus must be in [0, 2]")
        if not MIN_CASES <= self.target_cases <= MAX_CASES:
            raise ScoreInputError(f"target_cases must be in {MIN_CASES}..{MAX_CASES}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ScoreParams:
        return cls(
            min_panel=int(raw["min_panel"]),  # type: ignore[call-overload]
            min_solver_owners=int(raw["min_solver_owners"]),  # type: ignore[call-overload]
            solve_threshold=float(raw["solve_threshold"]),  # type: ignore[arg-type]
            champion_bonus=float(raw["champion_bonus"]),  # type: ignore[arg-type]
            target_cases=int(raw["target_cases"]),  # type: ignore[call-overload]
        )


@dataclass(frozen=True)
class CaseRef:
    case_id: str
    fingerprint: str


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    score: float
    fault: bool = False


@dataclass(frozen=True)
class PanelMember:
    agent_id: str
    owner_key: str
    champion: bool = False
    results: Sequence[CaseResult] = ()


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    fingerprint: str
    status: str = ""
    panel_size: int = 0
    solvers: int = 0
    solver_owners: int = 0
    mean_score: float = 0.0
    champion_failed: bool = False
    credit: float = 0.0


@dataclass(frozen=True)
class PackScore:
    status: str
    score: float
    raw_credit: float
    discriminating: int
    trivial: int
    unsolved: int
    duplicate: int
    unsolved_rate: float
    cases: tuple[CaseVerdict, ...] = field(default=())


def score_cases(
    cases: Sequence[CaseRef],
    panel: Sequence[PanelMember],
    params: ScoreParams,
    *,
    generator_owner_key: str = "",
    claimed_fingerprints: Sequence[str] = (),
) -> PackScore:
    """Judge a validated pack's cases against panel results (see module doc)."""
    params.validate()
    ordered = sorted(panel, key=lambda m: m.agent_id)
    by_agent: list[dict[str, CaseResult]] = []
    for index, member in enumerate(ordered):
        if not member.agent_id:
            raise ScoreInputError("panel member has an empty agent_id")
        if index > 0 and ordered[index - 1].agent_id == member.agent_id:
            raise ScoreInputError(f"duplicate panel agent {member.agent_id!r}")
        if not member.owner_key:
            # Owners decide independence; a blank owner would merge unrelated
            # agents into one solver.
            raise ScoreInputError(
                f"panel member {member.agent_id!r} has an empty owner_key"
            )
        results: dict[str, CaseResult] = {}
        for entry in member.results:
            if math.isnan(entry.score) or not 0 <= entry.score <= 1:
                raise ScoreInputError(
                    f"agent {member.agent_id!r} case {entry.case_id!r} "
                    "score out of range"
                )
            if entry.case_id in results:
                raise ScoreInputError(
                    f"agent {member.agent_id!r} has two results for case "
                    f"{entry.case_id!r}"
                )
            results[entry.case_id] = entry
        by_agent.append(results)
    claimed = set(claimed_fingerprints)

    status = PACK_SCORED
    raw_credit = 0.0
    counts = {
        CASE_DISCRIMINATING: 0,
        CASE_TRIVIAL: 0,
        CASE_UNSOLVED: 0,
        CASE_DUPLICATE: 0,
    }
    verdicts: list[CaseVerdict] = []
    # Results are looked up by case id, so one id naming two fingerprints would
    # let one panel result earn credit twice.
    fingerprint_of: dict[str, str] = {}
    for case in cases:
        if fingerprint_of.setdefault(case.case_id, case.fingerprint) != (
            case.fingerprint
        ):
            raise ScoreInputError(
                f"case {case.case_id!r} names two different fingerprints"
            )
    for case in cases:
        if case.fingerprint in claimed:
            counts[CASE_DUPLICATE] += 1
            verdicts.append(
                CaseVerdict(case.case_id, case.fingerprint, status=CASE_DUPLICATE)
            )
            continue
        # A repeat within the same pack is a duplicate too: one case content
        # earns credit once.
        claimed.add(case.fingerprint)
        total = 0.0
        panel_size = 0
        solvers = 0
        solver_owners: set[str] = set()
        champion_failed = False
        for member, results in zip(ordered, by_agent, strict=True):
            if generator_owner_key and member.owner_key == generator_owner_key:
                continue
            result = results.get(case.case_id)
            if result is None or result.fault:
                continue
            panel_size += 1
            total += result.score
            if result.score >= params.solve_threshold:
                solvers += 1
                solver_owners.add(member.owner_key)
            elif member.champion:
                champion_failed = True
        mean = total / panel_size if panel_size else 0.0
        credit = 0.0
        if panel_size < params.min_panel:
            case_status = CASE_INSUFFICIENT_PANEL
            status = PACK_INSUFFICIENT_PANEL
        elif len(solver_owners) < params.min_solver_owners:
            case_status = CASE_UNSOLVED
        elif solvers == panel_size:
            case_status = CASE_TRIVIAL
        else:
            case_status = CASE_DISCRIMINATING
            credit = 1 - mean
            if champion_failed:
                credit *= 1 + params.champion_bonus
            raw_credit += credit
        if case_status in counts:
            counts[case_status] += 1
        verdicts.append(
            CaseVerdict(
                case_id=case.case_id,
                fingerprint=case.fingerprint,
                status=case_status,
                panel_size=panel_size,
                solvers=solvers,
                solver_owners=len(solver_owners),
                mean_score=mean,
                champion_failed=champion_failed,
                credit=credit,
            )
        )

    if status != PACK_SCORED:
        return PackScore(
            status=status,
            score=0.0,
            raw_credit=0.0,
            discriminating=counts[CASE_DISCRIMINATING],
            trivial=counts[CASE_TRIVIAL],
            unsolved=counts[CASE_UNSOLVED],
            duplicate=counts[CASE_DUPLICATE],
            unsolved_rate=0.0,
            cases=tuple(CaseVerdict(**{**v.__dict__, "credit": 0.0}) for v in verdicts),
        )
    considered = len(cases) - counts[CASE_DUPLICATE]
    unsolved_rate = counts[CASE_UNSOLVED] / considered if considered > 0 else 0.0
    denom = max(len(cases), params.target_cases)
    solvable = 1 - unsolved_rate
    score = raw_credit / ((1 + params.champion_bonus) * denom) * solvable * solvable
    return PackScore(
        status=status,
        score=min(score, 1.0),
        raw_credit=raw_credit,
        discriminating=counts[CASE_DISCRIMINATING],
        trivial=counts[CASE_TRIVIAL],
        unsolved=counts[CASE_UNSOLVED],
        duplicate=counts[CASE_DUPLICATE],
        unsolved_rate=unsolved_rate,
        cases=tuple(verdicts),
    )


@dataclass(frozen=True)
class SeedPanel:
    seed: str
    cases: Sequence[CaseRef]
    panel: Sequence[PanelMember]


@dataclass(frozen=True)
class RunScore:
    status: str
    score: float
    seeds: tuple[PackScore, ...] = field(default=())


def score_run(
    seeds: Sequence[SeedPanel],
    params: ScoreParams,
    *,
    generator_owner_key: str = "",
    claimed_fingerprints: Sequence[str] = (),
) -> RunScore:
    """Score one generator evaluation: each seed's pack, then the mean.

    Every fingerprint a seed emits is claimed for later seeds, so a generator
    that ignores its seed scores duplicates after its first pack. Any seed with
    an insufficient panel makes the run insufficient (no score).
    """
    if not seeds:
        raise ScoreInputError("run has no seeds")
    claimed = list(claimed_fingerprints)
    status = PACK_SCORED
    total = 0.0
    scored: list[PackScore] = []
    for seed in seeds:
        pack = score_cases(
            seed.cases,
            seed.panel,
            params,
            generator_owner_key=generator_owner_key,
            claimed_fingerprints=claimed,
        )
        if pack.status != PACK_SCORED:
            status = PACK_INSUFFICIENT_PANEL
        total += pack.score
        scored.append(pack)
        claimed.extend(case.fingerprint for case in seed.cases)
    if status != PACK_SCORED:
        return RunScore(status=status, score=0.0, seeds=tuple(scored))
    return RunScore(status=status, score=total / len(seeds), seeds=tuple(scored))
