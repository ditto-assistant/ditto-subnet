"""Automated clear/reject adjudication of a held source review.

L1/L2/L3 answer "is there something here". They are deliberately bad at
answering "does it clear the bar", because a lead is cheap and a verdict is
expensive: the reviewer is told to record a concern the moment it sees one,
and the finding contract then over-flags a recurring set of legitimate
patterns. Everything they cannot resolve becomes an operator hold, and the
operator is the bottleneck.

This is that operator, as a bounded process. It receives the ledger the
earlier layers accumulated, re-reads the source those notes point at with the
same read-only tools, applies the published adjudication doctrine (the two-limb
refusal test, the production-engine test, the policy-v10 invariants, and the
court's known false positives), and returns ``clear`` or ``reject`` with a
reason and the ``path:line`` set it actually read.

It is a small fast model on purpose. The expensive discovery already happened
upstream; what is left is applying a written standard to named locations, and
a closed decision vocabulary plus host-side verification does more for that
than model size. The host checks every citation against the archive AND
against what this adjudicator actually read, so a decision resting on a
hallucinated or unread location is refused rather than executed.

A refused decision settles clear under the published no-proven-breach rule.
There is no path here that rejects on evidence the host could not verify, and
a bounded automated court can never strand a submission in an operator hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from ditto_screener.evidence_quality import citation_admissibility
from ditto_screener.source_review import (
    _OPENROUTER_ATTRIBUTION_HEADERS,
    TarSourceRepository,
    _execute_tool,
    _retryable_model_error_type,
)
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    AdjudicationClearClause,
    SourceReviewAdjudication,
    SourceReviewCitation,
    SourceReviewInvariant,
)

logger = logging.getLogger(__name__)

_SUPPORTED_POLICY_VERSIONS = tuple(
    range(SCREENING_FLOOR_POLICY_VERSION, SCREENING_POLICY_VERSION + 1)
)


def adjudicator_prompt_revision(policy_version: int) -> str:
    """Return the court prompt revision for an implemented policy version."""
    if policy_version not in _SUPPORTED_POLICY_VERSIONS:
        raise ValueError(
            "adjudicator policy v"
            f"{policy_version} is not implemented by this build "
            f"(implements {list(_SUPPORTED_POLICY_VERSIONS)})"
        )
    return f"adjudicator-v3-policy-v{policy_version}"


# Kept as the current-policy compatibility export for callers that only need
# the worker's default revision. Every actual court decision calls the
# versioned helper, so an activated older policy cannot be stamped as current.
ADJUDICATOR_PROMPT_REVISION = adjudicator_prompt_revision(SCREENING_POLICY_VERSION)
_DEFAULT_MODEL = "z-ai/glm-5.3-flash"
_MAX_STEPS = 128
_MAX_COMPLETION_TOKENS = 6_000
# A provider request must never consume an otherwise healthy screening lease.
# The court can resume its compacted ledger on one transient retry; after that,
# the published no-proven-breach rule settles the review rather than stranding
# the miner behind an unresponsive model endpoint.
# The decision-only court emits one tool call over preloaded evidence.  Keep a
# healthy completion responsive and reserve one equal slice for a new
# connection; an unresponsive provider must settle from retained notes, not
# spend 150 seconds of a miner's lease.
_MAX_COMPLETION_REQUEST_SECONDS = 45.0
_MAX_COMPLETION_REQUEST_ATTEMPTS = 2
# Bounded by the repository tools themselves; this only caps how many of
# the served locations are remembered for citation checking.
_MAX_RECORDED_READS = 2_048
_MAX_NOTES_IN_PROMPT = 48
_MAX_CITATIONS = 8
# L4 is the court, not a second source-review pass. Whenever an upstream layer
# retained exact source leads, preload those leads and require the court's
# decision in one bounded model turn. The only exception is a failure before
# any note was recorded: L4 may inspect the archive then, because there is no
# ledger for it to decide.
_MAX_PRELOADED_LEDGER_LOCATIONS = 16
_BUDGET_TERMINATED_REVIEW_CODES = frozenset(
    {
        "source-review-lease-budget-exhausted",
        "source-review-read-budget-exhausted",
        "source-review-step-budget-exhausted",
    }
)
_COMPACTED_TURNS_TO_KEEP = 3

_SYSTEM_PROMPT = """
You are the SN118 screening court's adjudicator. A bounded automated review
of an untrusted miner harness has finished without a decision an operator can
act on, and you are deciding it. Your output is FINAL: a clear admits this
submission to scoring, a reject is terminal for it.

You are given the review's notes ledger. Every note is a LEAD recorded
mid-inspection, never a verdict. The reviewer is instructed to record a
concern the moment it sees one, so concerns are cheap and frequently wrong.
Reviews that ran to completion and then concluded low risk still carried
concerns. You must re-read the source for yourself before deciding anything.

## Evidence priority

1. Served runtime dispatch and answer construction.
2. Exact reachable source locations.
3. Behavioural observations.
4. Artifact hashes, ownership, and duplicate manifests.
5. Score shape and miner claims: investigation leads only.

## The question you are actually answering

Under policy v10 the decisive breach is HOST AUTHORITY OVER THE GRADED SLOT.
Reject only when a reachable scored path does one of these:

- I3 model dissent. Content-based retry, suppression, or overwrite of a
  parseable model answer. Syntax, schema, empty-output and protocol-shape
  validation are legitimate.
- I4 derived-value authority. A host-derived value injected as an
  accept-until-match or reject-until-match gate over the model's answer, or
  labelled worked out / verified / exact / do-not-recompute. An explicitly
  untrusted candidate offered beside the complete records, with disagreement
  accepted and no correctness gate, passes.
- I5 production engine. A closed answer-family or operand recipe, value-kind
  registry, phrase table, semantic sheet, generator-fitted typo ladder, or
  family-specific instruction compiler. The model writing the final string
  does not save it. Ask: if this user renamed every field and paraphrased the
  request tomorrow, would this still be a general assistant?
- I7 model tool planning. An enforced plan that predetermines the executed
  tool count, set, or order against a deciding turn that cannot deviate, skip,
  add, or reorder. Advisory intent signals, grammar routers, precursor passes
  and trimmed-but-complete catalogues pass.
  A host-selected `required_*tool` passed through a `ForcedChoiceModel` (or an
  equivalent exact provider choice) is enforced, not advisory. So is a
  follow-up that checks whether the model called a named tool and then orders
  it to call that tool once while forbidding every other tool. A broader
  catalog on an earlier turn or an intent classifier that selected the plan
  does not restore the deciding turn's freedom to deviate.

Also reject on I1/I2/I6 when the served path never calls the model, withholds
the current user's records from the deciding turn, or reports tool calls that
did not execute.

Score each independently. One invariant passing does not park another
breaching. A real model call alone never displaces I3, I4, I5 or I7.

## Known false positives -- clear these with the named clause

The upstream layers flag all of these repeatedly and they are NOT violations:

- retrieval_ranking_not_family_engine: group rankers, cross-encoder reranking,
  candidate-pool tuning, fused vector retrieval. Legitimate context expansion.
  (But a static prompt enumerating the benchmark's per-family operand formulas
  and output formats IS a real I5 compiler even as prose. Read the text.)
- content_complete_memoization_cache: a cache whose key includes a system
  prompt that already embeds this user's retrieved records is user-specific by
  construction.
- standard_broker_inference_client: reading the harness's own
  OPENROUTER_API_KEY and preferring the injected DITTOBENCH_INFERENCE_BASE_URL
  broker is the intended audited path.
- unreported_tool_calls_executed: reporting an empty tool_calls list while
  actually executing calls against the supplied endpoint. The observed broker
  trajectory is authoritative; declining to self-report is not fabrication.
- local_practice_harness_stub: a stub that only fires when no tool_endpoint is
  supplied is unreachable on the scored path.
- intent_routing_or_precursor_pass: routers, per-intent classifiers, advisory
  relevance signals, and informing pre-passes, when the deciding model still
  sees the catalogue and makes the calls.
- bench_version_branching_alone: branching on bench_version is a warning sign
  that requires a companion violation, never an independent reject.
- single_success_duplicate_suppression: suppressing a repeat of a
  side-effecting tool after a genuine first success is production idempotency.
- plain_answer_normalization: unicode folding, slot-length caps and whitespace
  normalization of the model's OWN output are scorer-fidelity gaps.
- prior_pattern_removed: a resubmission that removed the previously rejected
  gate is a good-faith fix, not the same violation.
- model_authors_graded_slot: nothing in the served path takes authority over
  the answer; the model writes it from this user's records.

Bugs are not violations. An honest agent that scores badly is not a cheat.
Infrastructure failures are never miner violations.

## Procedure

1. Read the ledger you were given and pick the served path it points at.
2. Follow that path in source with read_file and search: entrypoint, request
   parsing, retrieval, the model call, tool dispatch, answer construction.
   Read the actual lines. You may only cite a line you have read.
3. For each concern note, decide whether the source you read substantiates it
   or matches a false-positive class above.
4. Call submit_adjudication exactly once.

## Deciding

- reject: name the breached invariant in reject_invariant and cite the minimal
  decisive path:line set. Quote nothing that is a challenge value.
- clear: name the clause in clear_clause and cite the lines that refute the
  concern -- the ones showing the model still authors the graded slot from the
  current user's records.

Cite executable source. A comment, an import, a derive, a closing brace or a
test file cannot carry a behaviour and will be refused.

When the evidence is genuinely mixed you must still choose, and the safe
choice is clear: a wrong reject bans an honest miner, while a wrong clear
leaves a submission that the next review, the behavioural oracle, and the
scored path all still get to catch. Reject only what you can prove from lines
you read.

The reason field is shown to the miner. Make it specific and factual: what
the served path does, at which file and line, and why that does or does not
take authority over the graded answer.
""".strip()

_POLICY_V11_PROMPT_TAIL = """

## Policy v11 additions

Review the complete served path, including routing, tool dispatch, retries,
and response assembly. Under I3, a selection model that drafts answer content
or uses request meaning to select a candidate answer is substantive answer
authorship, even if a later model writes the final string. Under I4, every
scorer-visible value must remain visible to the deciding model; a hidden
derived value used as a gate is a breach. Under I5, benchmark-specific prompt
prose can be a family compiler just as executable code can.

Under I7, planner authorship does not save a forced executor. A deciding turn
must be able to choose a different valid tool, skip the proposed tool, add a
valid tool, or reorder valid tools. If host code removes any of those options,
the plan is enforced even if an earlier model or classifier selected it.
""".strip()


def _system_prompt(policy_version: int) -> str:
    """Render the court doctrine bound to the submission's policy version."""
    # Validate through the same canonical revision helper so a new Platform
    # policy cannot silently reuse an older court doctrine.
    adjudicator_prompt_revision(policy_version)
    if policy_version == 10:
        return _SYSTEM_PROMPT
    if policy_version == 11:
        return f"{_SYSTEM_PROMPT}\n\n{_POLICY_V11_PROMPT_TAIL}"
    raise AssertionError("validated policy was not rendered")


_TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List archive file paths under an optional prefix.",
            "parameters": {
                "type": "object",
                "properties": {"prefix": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a bounded line range from one UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Case-insensitive literal search over bounded text files.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_adjudication",
            "description": "Record the final clear or reject decision. Call once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["clear", "reject"]},
                    "reason": {"type": "string"},
                    "reject_invariant": {
                        "type": "string",
                        "enum": [item.value for item in SourceReviewInvariant],
                    },
                    "clear_clause": {
                        "type": "string",
                        "enum": [item.value for item in AdjudicationClearClause],
                    },
                    "citations": {
                        "type": "array",
                        "maxItems": _MAX_CITATIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "line": {"type": "integer", "minimum": 1},
                            },
                            "required": ["path", "line"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["decision", "reason", "citations"],
                "additionalProperties": False,
            },
        },
    },
]

# ``submit_adjudication`` is the final (and only decision-only) tool above.
# Keep the selected schema object rather than retyping a second contract.
_DECISION_ONLY_TOOLS = [_TOOLS[-1]]


@dataclass(frozen=True)
class _Verdict:
    """A model decision that has not yet been checked against the archive."""

    decision: str
    reason: str
    reject_invariant: str | None
    clear_clause: str | None
    citations: tuple[tuple[str, int], ...]


def _escalate(
    code: str,
    reason: str,
    *,
    model: str,
    notes: int,
    policy_version: int,
) -> SourceReviewAdjudication:
    return SourceReviewAdjudication(
        decision="escalate",
        reason=reason,
        model=model,
        prompt_revision=adjudicator_prompt_revision(policy_version),
        policy_version=policy_version,
        notes_considered=notes,
        escalation_code=code,
    )


def _settle_refusal(
    adjudication: SourceReviewAdjudication,
    *,
    model: str,
    notes: int,
    policy_version: int,
) -> SourceReviewAdjudication:
    """Turn a refused court output into the only fair terminal fallback.

    A missing, timed-out, exhausted, or malformed court result proves no miner
    breach.  Reject still requires verified executable citations; when that
    proof is absent, the published rule is to clear rather than park the
    submission indefinitely or punish the miner for reviewer infrastructure.
    """
    if adjudication.decision != "escalate":
        return adjudication
    refusal_code = adjudication.escalation_code or "court-refused"
    return SourceReviewAdjudication(
        decision="clear",
        reason=(
            f"Automated adjudication ended ({refusal_code}) without a verified "
            f"policy breach after considering {notes} persisted review notes; "
            "cleared under "
            "the no-proven-breach-before-deadline rule"
        ),
        clear_clause=AdjudicationClearClause.NO_PROVEN_BREACH,
        model=model,
        prompt_revision=adjudicator_prompt_revision(policy_version),
        policy_version=policy_version,
        notes_considered=notes,
        # This is a terminal clear, not an internal escalation, but retaining
        # the refusal code makes the signed private review evidence honest
        # about why the no-proven-breach rule settled the case.
        escalation_code=refusal_code,
    )


def _bounded_sequence(value: object, limit: int) -> list[object]:
    """Take at most ``limit`` items from untrusted model or finding JSON."""
    if not isinstance(value, list):
        return []
    return list(value[:limit])


def _ledger_brief(notes: Sequence[Mapping[str, object]]) -> str:
    """Render the accumulated ledger as the adjudicator's starting leads."""
    rows = []
    for note in list(notes)[:_MAX_NOTES_IN_PROMPT]:
        location = ""
        path = note.get("path")
        if isinstance(path, str) and path:
            line = note.get("line")
            location = f"{path}:{line}" if isinstance(line, int) else path
        rows.append(
            {
                "kind": note.get("kind"),
                "category": note.get("category"),
                "at": location,
                "summary": note.get("summary"),
            }
        )
    return json.dumps(rows, separators=(",", ":"))


def _finding_brief(finding: Mapping[str, object] | None) -> str:
    """Render the upstream finding, if any, as a lead and not a verdict."""
    if not isinstance(finding, Mapping):
        return "none"
    bounded = {
        "risk_level": finding.get("risk_level"),
        "categories": finding.get("categories"),
        "summary": finding.get("summary"),
        "evidence": [
            {
                "path": item.get("path"),
                "line": item.get("line"),
                "category": item.get("category"),
            }
            for item in _bounded_sequence(finding.get("evidence"), 16)
            if isinstance(item, Mapping)
        ],
    }
    return json.dumps(bounded, separators=(",", ":"))


def _preload_ledger_evidence(
    repository: TarSourceRepository,
    notes: Sequence[Mapping[str, object]],
) -> tuple[str, set[tuple[str, int]]]:
    """Return bounded source excerpts for the L4 decision-only path.

    L1/L2/L3's ledger gives exact leads. Asking L4 to rediscover an archive
    after any evidence-bearing inconclusive handoff can consume the renewable
    lease and still leave the miner waiting on a model timeout. The host
    preloads bounded ranges around those leads, records every delivered line,
    and gives the court only its final-decision tool. A reject remains
    fail-closed: its citations must still name an executable preloaded line.
    """
    outputs: list[str] = []
    read_locations: set[tuple[str, int]] = set()
    requested: set[tuple[str, int]] = set()
    for note in notes:
        path = note.get("path")
        line = note.get("line")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(line, int)
            or isinstance(line, bool)
            or not 1 <= line <= 1_000_000
        ):
            continue
        location = (path.removeprefix("./"), line)
        if location in requested:
            continue
        requested.add(location)
        if len(requested) > _MAX_PRELOADED_LEDGER_LOCATIONS:
            break
        try:
            output = _execute_tool(
                repository,
                "read_file",
                {
                    "path": location[0],
                    "start_line": max(1, line - 12),
                    "end_line": line + 12,
                },
            )
        except ValueError:
            continue
        _record_reads(output, read_locations)
        if read_locations:
            outputs.append(output)
    return "\n".join(outputs), read_locations


def _compacted_adjudicator_messages(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Bound court context while preserving the case brief and recent work.

    Read locations remain tracked independently for host-side citation
    verification. The model can re-read an older location if it still matters;
    a growing transcript must not become the adjudicator's effective budget.
    """
    assistant_indices = [
        index
        for index, message in enumerate(messages)
        if index >= 2 and message.get("role") == "assistant"
    ]
    if len(assistant_indices) <= _COMPACTED_TURNS_TO_KEEP:
        return messages
    cutoff = assistant_indices[-_COMPACTED_TURNS_TO_KEEP]
    return [
        *messages[:2],
        {
            "role": "user",
            "content": (
                "[Earlier inspection turns were compacted. The original case "
                "brief above remains authoritative. Re-read any older source "
                "location needed for the final decision.]"
            ),
        },
        *messages[cutoff:],
    ]


class SourceReviewAdjudicator:
    """Small tool-using court with no shell, edit, execution, or web tools."""

    def __init__(
        self,
        *,
        api_key_file: str | None,
        base_url: str,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: float = 600.0,
        max_steps: int = _MAX_STEPS,
        max_completion_tokens: int = _MAX_COMPLETION_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key_file = api_key_file
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_steps = max(1, int(max_steps))
        self._max_completion_tokens = max(1_000, int(max_completion_tokens))
        self._transport = transport

    async def adjudicate(
        self,
        archive_path: str,
        *,
        notes: Sequence[Mapping[str, object]],
        finding: Mapping[str, object] | None = None,
        error_code: str | None = None,
        deadline: float | None = None,
        policy_version: int = SCREENING_POLICY_VERSION,
        ledger_final: bool = False,
    ) -> SourceReviewAdjudication:
        """Decide one held review. Never raises and always settles terminally."""
        note_count = len(notes)
        try:
            api_key = self._read_api_key()
            repository = TarSourceRepository(archive_path)
        except (OSError, ValueError) as error:
            logger.warning("adjudication could not start: %s", error)
            return _settle_refusal(
                _escalate(
                    "adjudicator-unavailable",
                    "Automated adjudication was unavailable",
                    model=self._model,
                    notes=note_count,
                    policy_version=policy_version,
                ),
                model=self._model,
                notes=note_count,
                policy_version=policy_version,
            )
        # The retained ledger is the court record.  It is independent of why
        # an upstream review stopped: a contradictory L2/L3 verdict with notes
        # is no reason to repeat L1's archive walk and burn the lease a second
        # time. ``ledger_final`` marks the production layered handoff; direct
        # callers without that marker retain the inspectable court path used by
        # unit and manual-review tooling.
        decision_only = bool(notes) and (ledger_final or error_code is not None)
        preloaded_evidence = ""
        preloaded_reads: set[tuple[str, int]] = set()
        if not notes and error_code in _BUDGET_TERMINATED_REVIEW_CODES:
            # An upstream review consumed its discovery budget without
            # recording evidence. There is nothing for the court to decide;
            # settle rather than spend its reserve rediscovering the archive.
            return _settle_refusal(
                _escalate(
                    "adjudicator-no-evidence",
                    "Automated adjudication received no retained source evidence",
                    model=self._model,
                    notes=note_count,
                    policy_version=policy_version,
                ),
                model=self._model,
                notes=note_count,
                policy_version=policy_version,
            )
        if decision_only:
            preloaded_evidence, preloaded_reads = _preload_ledger_evidence(
                repository, notes
            )
            if not preloaded_evidence:
                # The upstream layers retained a ledger but no usable source
                # evidence. There is nothing for a court to decide; do not
                # burn its reserve rediscovering the archive. This is still the
                # terminal no-proven-breach adjudication, not a retry.
                return _settle_refusal(
                    _escalate(
                        "adjudicator-no-evidence",
                        "Automated adjudication received no retained source evidence",
                        model=self._model,
                        notes=note_count,
                        policy_version=policy_version,
                    ),
                    model=self._model,
                    notes=note_count,
                    policy_version=policy_version,
                )
        try:
            verdict, read_locations = await self._run(
                repository,
                api_key,
                notes=notes,
                finding=finding,
                error_code=error_code,
                deadline=deadline,
                policy_version=policy_version,
                decision_only=decision_only,
                preloaded_evidence=preloaded_evidence,
                preloaded_reads=preloaded_reads,
            )
        except (
            OSError,
            TimeoutError,
            ValueError,
            httpx.HTTPError,
            json.JSONDecodeError,
        ) as error:
            logger.warning(
                "adjudication failed model=%s cause=%s: %s",
                self._model,
                type(error).__name__,
                error,
            )
            return _settle_refusal(
                _escalate(
                    "adjudicator-failed",
                    "Automated adjudication did not complete",
                    model=self._model,
                    notes=note_count,
                    policy_version=policy_version,
                ),
                model=self._model,
                notes=note_count,
                policy_version=policy_version,
            )
        return _settle_refusal(
            self._certify(
                verdict,
                repository=repository,
                read_locations=read_locations,
                notes=note_count,
                policy_version=policy_version,
            ),
            model=self._model,
            notes=note_count,
            policy_version=policy_version,
        )

    def _certify(
        self,
        verdict: _Verdict,
        *,
        repository: TarSourceRepository,
        read_locations: set[tuple[str, int]],
        notes: int,
        policy_version: int,
    ) -> SourceReviewAdjudication:
        """Refuse any decision the host cannot verify against the archive.

        This is the whole safety argument for using a small model here. The
        decision itself is cheap to check: the citations have to exist, have to
        be code, and have to be locations this adjudicator actually opened.
        """
        if not verdict.citations:
            return _escalate(
                "uncited-decision",
                "Automated adjudication cited no source; held for operator review",
                model=self._model,
                notes=notes,
                policy_version=policy_version,
            )
        admissible: list[SourceReviewCitation] = []
        for path, line in verdict.citations:
            normalized = path.removeprefix("./")
            if not repository.has_member(normalized):
                return _escalate(
                    "cited-unknown-member",
                    "Automated adjudication cited a path outside the submission; "
                    "held for operator review",
                    model=self._model,
                    notes=notes,
                    policy_version=policy_version,
                )
            if (normalized, line) not in read_locations:
                # This subsumes a bounds check: the tools only ever serve real
                # lines, so a citation past the end of a file was necessarily
                # never read either and lands here.
                return _escalate(
                    "cited-unread-source",
                    "Automated adjudication cited source it did not read; held "
                    "for operator review",
                    model=self._model,
                    notes=notes,
                    policy_version=policy_version,
                )
            if citation_admissibility(
                normalized, repository.member_text(normalized), line
            ).admissible:
                admissible.append(SourceReviewCitation(path=normalized, line=line))
        if not admissible:
            # Comments, imports, closing braces and test paths cannot carry a
            # behaviour, so a decision resting only on them rests on nothing.
            return _escalate(
                "inadmissible-citations",
                "Automated adjudication cited no executable source; held for "
                "operator review",
                model=self._model,
                notes=notes,
                policy_version=policy_version,
            )
        try:
            return SourceReviewAdjudication(
                decision=verdict.decision,
                reason=verdict.reason,
                reject_invariant=(
                    SourceReviewInvariant(verdict.reject_invariant)
                    if verdict.reject_invariant
                    else None
                ),
                clear_clause=(
                    AdjudicationClearClause(verdict.clear_clause)
                    if verdict.clear_clause
                    else None
                ),
                citations=admissible[:_MAX_CITATIONS],
                notes_considered=notes,
                model=self._model,
                prompt_revision=adjudicator_prompt_revision(policy_version),
                policy_version=policy_version,
            )
        except ValueError as error:
            logger.warning("adjudication verdict was self-inconsistent: %s", error)
            return _escalate(
                "verdict-contract-failed",
                "Automated adjudication did not name a published basis; held for "
                "operator review",
                model=self._model,
                notes=notes,
                policy_version=policy_version,
            )

    def _read_api_key(self) -> str:
        if not self._api_key_file:
            raise OSError("adjudicator API key file is not configured")
        path = Path(self._api_key_file)
        if path.stat().st_mode & 0o077:
            raise OSError("adjudicator API key file permissions are too broad")
        key = path.read_text().strip()
        if len(key) < 20:
            raise OSError("adjudicator API key is unavailable")
        return key

    async def _run(
        self,
        repository: TarSourceRepository,
        api_key: str,
        *,
        notes: Sequence[Mapping[str, object]],
        finding: Mapping[str, object] | None,
        error_code: str | None,
        deadline: float | None,
        policy_version: int,
        decision_only: bool = False,
        preloaded_evidence: str = "",
        preloaded_reads: set[tuple[str, int]] | None = None,
    ) -> tuple[_Verdict, set[tuple[str, int]]]:
        decision_only_instruction = (
            "\nThe host preloaded the exact source excerpts for the retained "
            "ledger. Decide from those excerpts now. Discovery tools are disabled; "
            "call submit_adjudication exactly once."
            if decision_only
            else ""
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _system_prompt(policy_version)},
            {
                "role": "user",
                "content": (
                    "Adjudicate this held submission.\n"
                    f"Why the review stopped: {error_code or 'bounded review'}\n"
                    f"Upstream finding (a lead): {_finding_brief(finding)}\n"
                    f"Notes ledger (leads): {_ledger_brief(notes)}\n"
                    "Archive inventory:\n"
                    + repository.inventory()
                    + (
                        "\nPreloaded source evidence:\n" + preloaded_evidence
                        if preloaded_evidence
                        else ""
                    )
                    + decision_only_instruction
                ),
            },
        ]
        read_locations = set(preloaded_reads or ())
        tools = _DECISION_ONLY_TOOLS if decision_only else _TOOLS
        max_steps = 1 if decision_only else self._max_steps
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._timeout_seconds
        ) as client:
            for _step in range(max_steps):
                request_timeout = self._timeout_seconds
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise ValueError("adjudicator exceeded lease budget")
                    request_timeout = min(request_timeout, remaining)
                # ``_completion_message`` may make one transient retry.  The
                # enclosing timeout keeps both requests inside the remaining
                # court window rather than letting the retry report after the
                # Platform lease is already gone.
                async with asyncio.timeout(request_timeout):
                    message = await self._completion_message(
                        client,
                        api_key,
                        _compacted_adjudicator_messages(messages),
                        timeout=request_timeout,
                        tools=tools,
                    )
                messages.append(message)
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list) or not tool_calls:
                    if decision_only:
                        raise ValueError(
                            "decision-only adjudicator response omitted final tool call"
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Respond only with a tool call: read the source, "
                                "or call submit_adjudication with your decision."
                            ),
                        }
                    )
                    continue
                for call in tool_calls:
                    call_id, name, arguments = _tool_call(call)
                    if name == "submit_adjudication":
                        return _verdict_from(arguments), read_locations
                    if decision_only:
                        raise ValueError(
                            "decision-only adjudicator requested source discovery"
                        )
                    try:
                        output = _execute_tool(repository, name, arguments)
                    except ValueError as error:
                        output = json.dumps({"error": str(error)})
                    else:
                        _record_reads(output, read_locations)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": output,
                        }
                    )
        raise ValueError("adjudicator exceeded step budget")

    async def _completion_message(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        messages: list[dict[str, object]],
        *,
        timeout: float | None,
        tools: Sequence[Mapping[str, object]] = _TOOLS,
    ) -> dict[str, object]:
        request = {
            "model": self._model,
            "messages": messages,
            "tools": list(tools),
            # A free-form answer cannot settle the court and previously used
            # an entire provider turn before the corrective prompt below.
            # Every valid next action is one of these bounded tools, so make
            # that contract explicit for the fast path as well.
            "tool_choice": "required",
            # OpenRouter advertises GLM 5.3 Flash's completion ceiling as
            # ``max_tokens``. With require_parameters enabled, sending the
            # OpenAI-specific alias filters every eligible endpoint and the
            # router returns a misleading 404.
            "max_tokens": self._max_completion_tokens,
            "provider": {
                # Preserve the same model and strict privacy/tool contract,
                # while allowing the router to fail over between compatible
                # healthy providers instead of timing out behind one endpoint.
                "allow_fallbacks": True,
                "sort": "throughput",
                "zdr": True,
                "data_collection": "deny",
                "require_parameters": True,
            },
        }
        effective_timeout = min(
            timeout if timeout is not None else self._timeout_seconds,
            _MAX_COMPLETION_REQUEST_SECONDS,
        )
        for attempt in range(_MAX_COMPLETION_REQUEST_ATTEMPTS):
            try:
                async with asyncio.timeout(effective_timeout):
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            **_OPENROUTER_ATTRIBUTION_HEADERS,
                        },
                        json=request,
                        timeout=effective_timeout,
                    )
            except (TimeoutError, httpx.TimeoutException):
                if attempt + 1 == _MAX_COMPLETION_REQUEST_ATTEMPTS:
                    raise
                logger.warning(
                    "adjudicator completion timed out; retrying once model=%s",
                    self._model,
                )
                continue
            break
        if response.status_code >= 400:
            response.raise_for_status()
        payload: object = response.json()
        if _retryable_model_error_type(payload) is not None:
            raise ValueError("adjudicator model body was unusable")
        return _assistant_message(payload)


def _assistant_message(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("adjudicator response is not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("adjudicator response has no choice")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("adjudicator response has no message")
    return message


def _tool_call(call: object) -> tuple[str, str, dict[str, object]]:
    if not isinstance(call, dict) or not isinstance(call.get("id"), str):
        raise ValueError("adjudicator tool call is invalid")
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ValueError("adjudicator function call is invalid")
    raw = function.get("arguments")
    if not isinstance(raw, str):
        raise ValueError("adjudicator arguments are invalid")
    arguments = json.loads(raw)
    if not isinstance(arguments, dict):
        raise ValueError("adjudicator arguments are not an object")
    return call["id"], function["name"], arguments


def _record_reads(output: str, seen: set[tuple[str, int]]) -> None:
    """Record the exact locations the adjudicator was actually shown.

    Both tools return the line numbers they served, so this is read back out
    of the tool result rather than inferred from the request: a ``read_file``
    range is clamped host-side, a miss returns an error object, and an
    oversized payload is replaced wholesale by a truncation stub. In every one
    of those cases nothing is credited, which is what makes "you may only cite
    a line you have read" enforceable instead of advisory.
    """
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    path = payload.get("path")
    if isinstance(path, str):
        for line in _bounded_sequence(payload.get("lines"), _MAX_RECORDED_READS):
            if isinstance(line, Mapping) and isinstance(line.get("line"), int):
                seen.add((path.removeprefix("./"), int(line["line"])))
    for hit in _bounded_sequence(payload.get("hits"), _MAX_RECORDED_READS):
        if (
            isinstance(hit, Mapping)
            and isinstance(hit.get("path"), str)
            and isinstance(hit.get("line"), int)
        ):
            seen.add((str(hit["path"]).removeprefix("./"), int(hit["line"])))


def _verdict_from(arguments: Mapping[str, object]) -> _Verdict:
    decision = arguments.get("decision")
    if decision not in {"clear", "reject"}:
        raise ValueError("adjudicator decision is invalid")
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("adjudicator decision has no reason")
    citations: list[tuple[str, int]] = []
    for item in _bounded_sequence(arguments.get("citations"), _MAX_CITATIONS):
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        line = item.get("line")
        if (
            isinstance(path, str)
            and path
            and isinstance(line, int)
            and not isinstance(line, bool)
            and line >= 1
        ):
            citations.append((path, line))
    invariant = arguments.get("reject_invariant")
    clause = arguments.get("clear_clause")
    return _Verdict(
        decision=decision,
        reason=" ".join(reason.split())[:600],
        reject_invariant=invariant if isinstance(invariant, str) else None,
        clear_clause=clause if isinstance(clause, str) else None,
        citations=tuple(citations),
    )


def build_adjudicator(config: object) -> SourceReviewAdjudicator | None:
    """Construct the court from screener config, or ``None`` when it is off.

    The adjudicator is off by default. It resolves holds terminally, so
    turning it on is an explicit operator act with an audited settings
    revision behind it, exactly like enabling L2/L3 review was.
    """
    mode = str(getattr(config, "adjudicator_mode", "off"))
    if mode == "off":
        return None
    return SourceReviewAdjudicator(
        api_key_file=getattr(config, "source_review_api_key_file", None),
        base_url=str(getattr(config, "source_review_base_url", "")),
        model=str(getattr(config, "adjudicator_model", _DEFAULT_MODEL)),
        timeout_seconds=float(getattr(config, "adjudicator_timeout_seconds", 600.0)),
        max_steps=int(getattr(config, "adjudicator_max_steps", _MAX_STEPS)),
        # Review settings expose a single, audited completion ceiling for the
        # paid deep-review path.  The court used to ignore it and silently
        # retain its 6k constructor default, even when the canary explicitly
        # granted 16k.  L4 is a consumer of that same bounded budget.
        max_completion_tokens=int(
            getattr(config, "l2_max_completion_tokens", _MAX_COMPLETION_TOKENS)
        ),
    )
