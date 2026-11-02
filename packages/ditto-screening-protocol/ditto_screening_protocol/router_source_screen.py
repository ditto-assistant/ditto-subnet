"""Canonical evidence and decision logic for the shadow router source screen.

The router competition grades a submission on a **public** pack the miner can
see and a disjoint **held-out** partition it cannot (see
``docs/router-compression-competition-v1.md``: the public pack carries
``task_entropy_bits = 0`` and the hidden partition is withheld and rotated). A
router that only works on the public set -- a deterministic lookup table keyed
on the visible tasks, a degenerate constant route, or a bench-tuned splice --
scores well in public and collapses on the held-out partition. This module is
the anti-bench-tuning brain: given the paired public/held-out signals for one
submission it decides whether the router *generalises*, and emits a signed,
content-addressed screen result mirroring
:mod:`ditto_screening_protocol.coding_source_screen`.

It is intentionally pure and dependency-light: no I/O, no scoring, no ditto
imports. The screener worker feeds it aggregates and persists the evidence; the
validator stays stateless. It does not change scoring or the merged
``router-compression-competition-v1`` contract -- the router dimension remains
shadow-only (``weight_eligible = False``); this only decides pass/quarantine/deny
*within* that shadow dimension.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256 = r"^[0-9a-f]{64}$"
_RULE = r"^[a-z][a-z0-9-]{0,63}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

# Scores are basis points of the same-wave reference arm, 0..10_000, matching
# the contract's ``saving_bps`` / ``FinalRouterScore_bps`` range.
_BPS_MAX = 10_000

# A public arm that beats the held-out arm by at least this many bps is not
# generalising at all: the router recognised the visible tasks specifically.
GENERALIZATION_GAP_DENY_BPS = 5_000
# A smaller-but-still-large gap is suspicious rather than proven garbage: a human
# reviewer looks before it could ever leave shadow.
GENERALIZATION_GAP_QUARANTINE_BPS = 2_500
# Below this, the held-out arm is effectively at the floor.
HELDOUT_FLOOR_BPS = 2_000
# A public arm at or above this while the held-out arm is at the floor is the
# classic "works only on the public set" signature.
PUBLIC_HEALTHY_BPS = 6_000
# A router that emits one (or zero) distinct routing decision across at least
# this many held-out cases is a degenerate constant route, not a router.
DEGENERATE_MIN_CASES = 4


class RouterSourceScreenOutcome(StrEnum):
    PASS = "pass"
    DENY = "deny"
    QUARANTINE = "quarantine"
    ADVISORY = "advisory"
    INFRASTRUCTURE = "infrastructure"


class RouterSourceScreenSeverity(StrEnum):
    DENY = "deny"
    QUARANTINE = "quarantine"
    ADVISORY = "advisory"


class RouterSourceScreenFinding(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    rule_id: Annotated[str, Field(pattern=_RULE)]
    severity: RouterSourceScreenSeverity
    evidence_sha256: Annotated[str, Field(pattern=_SHA256)]


class RouterSourceScreenEvidence(BaseModel):
    """Content-addressed, source-safe router screening result.

    Carries no task text, no routing traces and no held-out task identities --
    only the digest of the metric detail that triggered each finding -- so
    publishing it can never leak the hidden partition to a miner.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_name: Literal["dittobench-router-source-screen-v1"] = Field(alias="schema")
    router_contract_version: Literal[1]
    weight_eligible: Literal[False]
    agent_artifact_sha256: Annotated[str, Field(pattern=_SHA256)]
    screened_image_sha256: Annotated[str, Field(pattern=_SHA256)]
    analyzer_version: Annotated[str, Field(pattern=_RULE)]
    policy_version: Annotated[int, Field(ge=1, le=1_000_000)]
    outcome: RouterSourceScreenOutcome
    findings: Annotated[tuple[RouterSourceScreenFinding, ...], Field(max_length=64)]
    evidence_sha256: Annotated[str, Field(pattern=_SHA256)]

    @model_validator(mode="after")
    def coherent(self) -> RouterSourceScreenEvidence:
        severities = {finding.severity for finding in self.findings}
        keys = [(finding.rule_id, finding.evidence_sha256) for finding in self.findings]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("router source findings are not canonical")
        if (
            self.outcome is RouterSourceScreenOutcome.DENY
            and RouterSourceScreenSeverity.DENY not in severities
        ):
            raise ValueError("router source deny requires deterministic deny evidence")
        if (
            self.outcome is RouterSourceScreenOutcome.QUARANTINE
            and RouterSourceScreenSeverity.QUARANTINE not in severities
        ):
            raise ValueError("router source quarantine requires quarantine evidence")
        if (
            self.outcome is RouterSourceScreenOutcome.QUARANTINE
            and RouterSourceScreenSeverity.DENY in severities
        ):
            raise ValueError("router source quarantine cannot carry deny evidence")
        if self.outcome is RouterSourceScreenOutcome.PASS and severities:
            raise ValueError("router source pass cannot carry findings")
        if self.outcome is RouterSourceScreenOutcome.ADVISORY and (
            RouterSourceScreenSeverity.DENY in severities
            or RouterSourceScreenSeverity.QUARANTINE in severities
        ):
            raise ValueError("router source advisory result cannot carry deny evidence")
        if self.outcome is RouterSourceScreenOutcome.INFRASTRUCTURE and severities:
            raise ValueError(
                "router source infrastructure result cannot blame the miner"
            )
        if self.evidence_sha256 != router_source_screen_digest(self):
            raise ValueError("router source screen evidence digest mismatch")
        return self


def router_source_screen_digest(evidence: RouterSourceScreenEvidence) -> str:
    payload = evidence.model_dump(
        mode="json", by_alias=True, exclude={"evidence_sha256"}
    )
    body = (
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        + b"\n"
    )
    return hashlib.sha256(body).hexdigest()


def router_source_screen_signing_message(
    *, screener_hotkey: str, evidence: RouterSourceScreenEvidence
) -> bytes:
    """Bind a screener signature to one canonical evidence result."""

    if not re.fullmatch(_SS58, screener_hotkey):
        raise ValueError("router source screen signer is invalid")
    return "\x00".join(
        (
            "dittobench-router-source-screen:v1",
            screener_hotkey,
            evidence.agent_artifact_sha256,
            evidence.screened_image_sha256,
            evidence.evidence_sha256,
        )
    ).encode()


class RouterGeneralizationSample(BaseModel):
    """The paired public/held-out signals for one router submission.

    Every field is an aggregate the screener already has from the paired scoring
    arms; none of it identifies a held-out task. The held-out arm is the arm the
    miner could not see and cannot tune against.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    public_score_bps: Annotated[int, Field(ge=0, le=_BPS_MAX)]
    """Median router score on the visible public pack, in bps of the reference."""
    heldout_score_bps: Annotated[int, Field(ge=0, le=_BPS_MAX)]
    """Median router score on the disjoint, withheld, rotating partition."""
    heldout_cases: Annotated[int, Field(ge=0, le=100_000)]
    """How many held-out cases the router was scored on."""
    distinct_heldout_routes: Annotated[int, Field(ge=0, le=100_000)]
    """Distinct routing decisions emitted across the held-out cases. One (or
    zero) across many cases is a constant/degenerate route, not a router."""
    canary_tasks_total: Annotated[int, Field(ge=0, le=100_000)] = 0
    """Held-out canary tasks: solvable by any genuine router, planted to catch a
    public-set lookup table that has no behaviour for unseen inputs."""
    canary_tasks_failed: Annotated[int, Field(ge=0, le=100_000)] = 0
    """Canary tasks the submission failed."""
    emitted_novel_token: bool = False
    """The contract forbids introducing tokens the reference arm never produced;
    True means the router injected novel tokens into the relayed stream."""
    deterministic_replay: bool = True
    """The contract requires a router to be deterministic under a fixed seed;
    False means a seeded replay diverged from the original run."""

    @model_validator(mode="after")
    def counts_are_coherent(self) -> RouterGeneralizationSample:
        if self.canary_tasks_failed > self.canary_tasks_total:
            raise ValueError("router canary failures exceed canary total")
        if self.distinct_heldout_routes > self.heldout_cases:
            raise ValueError("distinct held-out routes exceed held-out cases")
        return self


def _finding(
    rule_id: str, severity: RouterSourceScreenSeverity, detail: dict[str, Any]
) -> RouterSourceScreenFinding:
    body = (
        json.dumps(
            {"rule": rule_id, **detail},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )
    return RouterSourceScreenFinding(
        rule_id=rule_id,
        severity=severity,
        evidence_sha256=hashlib.sha256(body).hexdigest(),
    )


def evaluate_router_generalization(
    sample: RouterGeneralizationSample,
) -> tuple[RouterSourceScreenOutcome, tuple[RouterSourceScreenFinding, ...]]:
    """Decide whether a router generalises off the public set.

    Pure and total: same input, same outcome and findings. The findings are
    returned in canonical (sorted, de-duplicated) order so they drop straight
    into :func:`build_router_source_screen_evidence`. The outcome is the worst
    severity present: any deny finding denies, else any quarantine finding
    quarantines, else pass.
    """

    findings: list[RouterSourceScreenFinding] = []

    # Hard contract violations first -- these are deterministic garbage, not a
    # judgement call about generalisation.
    if sample.emitted_novel_token:
        findings.append(
            _finding(
                "novel-token-emitted",
                RouterSourceScreenSeverity.DENY,
                {"emitted_novel_token": True},
            )
        )
    if sample.canary_tasks_failed > 0:
        findings.append(
            _finding(
                "heldout-canary-failed",
                RouterSourceScreenSeverity.DENY,
                {
                    "failed": sample.canary_tasks_failed,
                    "total": sample.canary_tasks_total,
                },
            )
        )
    if (
        sample.heldout_cases >= DEGENERATE_MIN_CASES
        and sample.distinct_heldout_routes <= 1
    ):
        findings.append(
            _finding(
                "degenerate-constant-route",
                RouterSourceScreenSeverity.DENY,
                {
                    "distinct_routes": sample.distinct_heldout_routes,
                    "cases": sample.heldout_cases,
                },
            )
        )

    gap = sample.public_score_bps - sample.heldout_score_bps
    if gap >= GENERALIZATION_GAP_DENY_BPS:
        findings.append(
            _finding(
                "generalization-gap",
                RouterSourceScreenSeverity.DENY,
                {"gap_bps": gap, "threshold_bps": GENERALIZATION_GAP_DENY_BPS},
            )
        )
    elif gap >= GENERALIZATION_GAP_QUARANTINE_BPS:
        findings.append(
            _finding(
                "generalization-gap",
                RouterSourceScreenSeverity.QUARANTINE,
                {"gap_bps": gap, "threshold_bps": GENERALIZATION_GAP_QUARANTINE_BPS},
            )
        )

    # Determinism is a quarantine, not a deny: a flaky router is not proven
    # bench-tuned, but it cannot be trusted on the held-out partition either.
    if not sample.deterministic_replay:
        findings.append(
            _finding(
                "nondeterministic-replay",
                RouterSourceScreenSeverity.QUARANTINE,
                {"deterministic_replay": False},
            )
        )
    if (
        sample.heldout_score_bps < HELDOUT_FLOOR_BPS
        and sample.public_score_bps >= PUBLIC_HEALTHY_BPS
    ):
        findings.append(
            _finding(
                "heldout-below-floor",
                RouterSourceScreenSeverity.QUARANTINE,
                {
                    "heldout_bps": sample.heldout_score_bps,
                    "floor_bps": HELDOUT_FLOOR_BPS,
                },
            )
        )

    # Canonical order + de-dup: a rule can only fire once, and (rule_id,
    # evidence_sha256) must sort ascending for the evidence coherence check.
    unique = {(f.rule_id, f.evidence_sha256): f for f in findings}
    ordered = tuple(unique[key] for key in sorted(unique))

    severities = {f.severity for f in ordered}
    if RouterSourceScreenSeverity.DENY in severities:
        outcome = RouterSourceScreenOutcome.DENY
    elif RouterSourceScreenSeverity.QUARANTINE in severities:
        outcome = RouterSourceScreenOutcome.QUARANTINE
    else:
        outcome = RouterSourceScreenOutcome.PASS
    return outcome, ordered


def build_router_source_screen_evidence(
    *,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    analyzer_version: str,
    policy_version: int,
    outcome: RouterSourceScreenOutcome,
    findings: tuple[RouterSourceScreenFinding, ...],
) -> RouterSourceScreenEvidence:
    """Assemble a self-consistent, content-addressed screen result.

    Fills ``evidence_sha256`` with the canonical digest so the returned evidence
    validates. Pass the ``(outcome, findings)`` from
    :func:`evaluate_router_generalization` straight through.
    """

    raw: dict[str, Any] = {
        "schema": "dittobench-router-source-screen-v1",
        "router_contract_version": 1,
        "weight_eligible": False,
        "agent_artifact_sha256": agent_artifact_sha256,
        "screened_image_sha256": screened_image_sha256,
        "analyzer_version": analyzer_version,
        "policy_version": policy_version,
        "outcome": outcome,
        "findings": findings,
        "evidence_sha256": "0" * 64,
    }
    provisional = RouterSourceScreenEvidence.model_construct(**raw)
    raw["evidence_sha256"] = router_source_screen_digest(provisional)
    return RouterSourceScreenEvidence.model_validate(raw)


def screen_router_submission(
    *,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    analyzer_version: str,
    policy_version: int,
    sample: RouterGeneralizationSample | None,
) -> RouterSourceScreenEvidence:
    """Screen one submission's router dimension end to end.

    The single non-test entry point that ties
    :func:`evaluate_router_generalization` to
    :func:`build_router_source_screen_evidence`, so the screener worker has one
    pure call to produce signed, content-addressed evidence for a submission.

    ``sample is None`` is the **opt-in / yes-and** case: the submission advertised
    no router project (the ``/router/health`` probe returned ``unsupported``), so
    there is nothing to grade. That maps to a benign ``INFRASTRUCTURE`` outcome
    with **no findings** -- never a ``DENY`` and never a reward -- exactly like the
    coding track's ``UNSUPPORTED`` status: a submission that omits the router
    project is scored on the memory contract exactly as before, and including one
    is purely additive. An included submission is graded normally by
    :func:`evaluate_router_generalization`.

    Pure and total: no I/O, no scoring, no ``ditto`` imports; the compute
    destination (in-process screener, offloaded worker) calls this identically.
    """
    if sample is None:
        outcome: RouterSourceScreenOutcome = RouterSourceScreenOutcome.INFRASTRUCTURE
        findings: tuple[RouterSourceScreenFinding, ...] = ()
    else:
        outcome, findings = evaluate_router_generalization(sample)
    return build_router_source_screen_evidence(
        agent_artifact_sha256=agent_artifact_sha256,
        screened_image_sha256=screened_image_sha256,
        analyzer_version=analyzer_version,
        policy_version=policy_version,
        outcome=outcome,
        findings=findings,
    )
