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

A refused decision (no key, unreadable archive, no retained evidence, a
timeout, or a malformed verdict) stays ``escalate``. The policy engine
carries it as an operator hold, never as an admission: between 2026-08-30
and 2026-09-06 a refusal settled as a clear and 135 of the 156 newest
automated clears were court crashes with zero notes, so the champion and
most of the board were admitted without any review. A held submission is
visible in Backroom and resolvable in one call; a silent admission is not.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from pydantic import ValidationError

from ditto_screener.decision_path_prompt import DECISION_PATH_GUIDANCE
from ditto_screener.evidence_quality import citation_admissibility
from ditto_screener.source_review import (
    TarSourceRepository,
    _execute_tool,
    _retryable_model_error_type,
    review_gateway_headers,
)
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    AdjudicationClearClause,
    AdjudicationRunDiagnostic,
    SourceReviewAdjudication,
    SourceReviewCitation,
    SourceReviewInvariant,
)
from ditto_screening_protocol.models import source_review_invariants_for_policy

logger = logging.getLogger(__name__)

_ERROR_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _clear_request_trace() -> None:
    """Forget the previous request's metadata before issuing the next one.

    A failure with no readable response must leave the upstream unknown rather
    than inherit the last one that answered.
    """
    trace = _run_trace.get()
    if trace is not None:
        trace.prompt_tokens = None
        trace.completion_tokens = None
        trace.final_tool_call_returned = None
        trace.http_status = None
        trace.upstream = None


def _observe_upstream(payload: object) -> None:
    """Record which upstream served this response, if it named one.

    Read before anything that can reject the body, because a provider fault
    relayed inside an HTTP 200 is exactly the failure worth attributing to an
    upstream.

    The trace spans a whole court run, so :func:`_clear_request_trace` empties this
    at the start of every request and retry. Without that, a step that answered
    from one upstream would still be named when a later step times out with no
    response at all, which blames an upstream for a call it never served.
    """
    trace = _run_trace.get()
    if trace is None or not isinstance(payload, dict):
        return
    upstream = _upstream_slug(payload.get("provider"))
    if upstream is not None:
        trace.upstream = upstream


def _upstream_slug(value: object) -> str | None:
    """Normalize the serving upstream's name, or ``None`` when it is unusable.

    The gateway reports names like ``Sail Research`` and ``Io Net``. Lowercase
    them and join the words so the value fits the same bounded slug every other
    identifier in this trace uses; anything that still does not fit is dropped
    rather than stored, because this string comes back from a provider response
    and only ever needs to be recognizable, never verbatim.
    """
    if not isinstance(value, str):
        return None
    slug = "-".join(value.strip().lower().split())
    return slug if _PROVIDER_RE.fullmatch(slug) else None


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
_RunStage = Literal["completion", "lease", "step-budget", "unavailable", "response"]


@dataclass
class _RunTrace:
    """Mutable, secret-free facts collected during one court attempt."""

    started: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    final_tool_call_returned: bool | None = None
    http_status: int | None = None
    upstream: str | None = None


_run_trace: contextvars.ContextVar[_RunTrace | None] = contextvars.ContextVar(
    "adjudicator_run_trace",
    default=None,
)

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
    if policy_version == 13:
        return "adjudicator-v7-policy-v13"
    return f"adjudicator-v4-policy-v{policy_version}"


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
# Reasoning plus a verdict can exceed 90s with a 16k completion budget.
# The outer request/lease deadline still bounds both attempts together.
_MAX_COMPLETION_REQUEST_SECONDS = 180.0
_MAX_COMPLETION_REQUEST_ATTEMPTS = 2
_MAX_COMPLETION_IDLE_SECONDS = 75.0
_MAX_COMPLETION_RESPONSE_BYTES = 512_000
# SSE repeats JSON framing for every token, and a 16k-token completion can
# exceed 2 MB of wire data even when its final tool call is small. This is a
# streaming transport ceiling, not a license to retain more model arguments:
# the separate 512 KB tool-data bound still applies.
_MAX_COMPLETION_STREAM_BYTES = 8_000_000


class CompletionWireTooLarge(ValueError):
    """The gateway streamed too much framing/content for one court turn."""


class CompletionToolTooLarge(ValueError):
    """The actual model tool-call data exceeded the strict verdict bound."""


class IncompleteStreamError(ValueError):
    """A transport ended before the gateway committed a complete response."""


class ProviderStreamError(ValueError):
    """A completed SSE frame explicitly reported a provider failure."""


class ProviderBodyError(ValueError):
    """A complete JSON body reported a retryable upstream failure."""


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
Use complete sentences and short paragraphs, up to 8000 characters. Do not
include source text, prompts, credentials, private challenge values, or transcripts.
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


_POLICY_V12_PROMPT_TAIL = """

## Policy v12 additions

The plain_answer_normalization false-positive class is NARROWED. It clears only
a transform that is request-independent (reads no request text, answer
contract, value-family or money/direction flag, or bench version) AND
content-preserving at the character level (Unicode/typography folding,
whitespace or quote trimming, length caps, `ANSWER:` prefix stripping) so that
every digit, sign, and alphabetic token the model wrote survives in order and
nothing the model did not write in the slot is inserted.

Reject for I4 (scorer_field_rewritten, effect answer or final_text) when the
served path, after the deciding model spoke, rescales or unit-converts a
model-authored number (bare minor-unit integer -> two-decimal currency through
`/ 100` and `% 100`, cents <-> dollars), maps a model-authored word onto a
canonical grader vocabulary (up/gain/loss/down -> increase/decrease), composes
or completes the slot from prose the model did not place in it, synthesizes
`answer` or `abstain` when the model emitted no slot, or runs any of these only
under a request-keyed flag (AnswerContract, include_minor_units,
money_representation, asks_direction, wants_minor_units). The model having
authored the input value is not no_derived_value; an untouched final_text is not
untrusted_candidate_channel; comments describing the rewrite as representation,
presentation, or typography folding are inadmissible. Cite the transform and
its call site on the served /run path and confirm the writer is not behind
cfg(test), a non-default cargo feature, or an unset environment variable.

Do not reject for this class when the only mechanism is a format-only model
re-ask triggered by slot shape alone (integer where a decimal wire type is
declared) that carries complete records, no derived value, and no conversion
recipe, and ships whatever parseable result the model returns. A prompt-text
conversion recipe remains the I5 family compiler policy v11 already names.
"""


_POLICY_V13_PROMPT_TAIL = """

## Policy v13 additions

Apply the mechanism-based Policy v13 checklist in docs/policy-v13.md. Decide an
exact artifact and effective build, not its names, lineage, labels, comments,
or dormant helpers. Conditionality is neither necessary nor sufficient for I5:
an always-on benchmark-specific recipe is activated on every request. Require
its generator-, fixture-, grader-, or evaluation-dependent assumption and
substantive effect; ordinary formulas, domain vocabulary, routing, and
prescriptive prose are insufficient.

I2 needs the exact applicable evidence and discard path. I3 permits a declared
genuine model critic/final selector with necessary evidence and real dissent
authority, but not a host accept-until-preferred loop. I4 includes conditional
deletion or relocation of answer fields, prose-derived abstention, currency
rescaling, added equivalents, and grader-vocabulary mapping after final model
authority. I7 includes unjustified removal of an applicable, authorized and
available capability, including quoted-title, compound-request, negation-scope,
setting-and-remember, and duplicate-call failures.

I8 is evaluation independence. Reject a complete source-proven branch where
case IDs, active seeds, expected answers, dataset hashes, scorer identity, audit
markers, or `bench_version` alter substantive retrieval, capability ordering,
authority, execution, or output beyond the published compatibility contract.
Behavioral degradation alone cannot reject until the published
post-commit controlled and replicated procedure attributes it to evaluation
dependence.

Opaque-file presence and feature-gated dead code are leads only. Establish the
loaded role and effective lifecycle reachability. Build/security findings may
end in unauthorized data access, disclosure, write, execution, persistence,
privilege, or material availability effects without involving a model or
scorer. Combine submission evidence with platform-supplied exact
path-and-digest provenance. A matched official starter-kit component satisfies
only the recorded fields and role; omission of its duplicate README or
metadata sidecar is not V1. Reverify changed configuration, loaders, candidate
boundaries, inputs, outputs, and downstream authority. A null compact score
field does not prove artifact-bound screening evidence is absent. A missing
predefined verification artifact or failed platform review
is not a proven integrity breach. If mandatory verification is incomplete, do
not manufacture a clear or a violation; call request_operator_review so the
host retains an escalate processing state for the operator's eventual
CLEAR/REJECT decision.
""".strip()


def _policy_v13_base_prompt() -> str:
    """Remove legacy directives that contradict v13's incomplete-review hold."""
    replacements = (
        (
            "Your output is FINAL: a clear admits this\n"
            "submission to scoring, a reject is terminal for it.",
            "A clear admits this submission to scoring, a reject is terminal for it, "
            "and request_operator_review keeps the submission held.",
        ),
        (
            "When the evidence is genuinely mixed you must still choose, and the safe\n"
            "choice is clear: a wrong reject bans an honest miner, while a "
            "wrong clear\n"
            "leaves a submission that the next review, the behavioural "
            "oracle, and the\n"
            "scored path all still get to catch. Reject only what you can prove "
            "from lines\n"
            "you read.",
            "When the evidence is genuinely mixed or mandatory verification is "
            "incomplete, call request_operator_review. Reject only what you can prove "
            "from lines you read; clear only after the required verification "
            "is complete.",
        ),
        (
            "4. Call submit_adjudication exactly once.",
            "4. Call submit_adjudication for a complete decision, or "
            "request_operator_review when verification is incomplete.",
        ),
    )
    prompt = _SYSTEM_PROMPT
    for old, new in replacements:
        if old not in prompt:
            raise AssertionError("v13 adjudicator base prompt drifted")
        prompt = prompt.replace(old, new, 1)
    return prompt


_POLICY_V13_BASE_PROMPT = _policy_v13_base_prompt()


def _system_prompt(policy_version: int) -> str:
    """Render the court doctrine bound to the submission's policy version."""
    # Validate through the same canonical revision helper so a new Platform
    # policy cannot silently reuse an older court doctrine.
    adjudicator_prompt_revision(policy_version)
    if policy_version == 10:
        return _SYSTEM_PROMPT
    if policy_version == 11:
        return f"{_SYSTEM_PROMPT}\n\n{_POLICY_V11_PROMPT_TAIL}"
    if policy_version == 12:
        return (
            f"{_SYSTEM_PROMPT}\n\n{_POLICY_V11_PROMPT_TAIL}\n\n"
            f"{_POLICY_V12_PROMPT_TAIL}"
        )
    if policy_version == 13:
        return (
            f"{_POLICY_V13_BASE_PROMPT}\n\n{_POLICY_V11_PROMPT_TAIL}\n\n"
            f"{_POLICY_V12_PROMPT_TAIL}\n\n{_POLICY_V13_PROMPT_TAIL}\n\n"
            f"{DECISION_PATH_GUIDANCE}"
        )
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
                    "reason": {"type": "string", "maxLength": 8000},
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

# Keep the selected verdict schema rather than retyping a second contract.
_DECISION_ONLY_TOOLS = [_TOOLS[-1]]

_OPERATOR_REVIEW_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "request_operator_review",
        "description": (
            "Keep an incomplete or mixed policy-v13 review held for an operator."
        ),
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 8000}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
}


def _adjudicator_tools_for_policy(
    policy_version: int, *, decision_only: bool = False
) -> list[dict[str, object]]:
    """Return a court schema restricted to the exact policy generation."""

    tools = copy.deepcopy(_DECISION_ONLY_TOOLS if decision_only else _TOOLS)
    if policy_version >= 13:
        tools.append(copy.deepcopy(_OPERATOR_REVIEW_TOOL))
    submit = None
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == "submit_adjudication":
            submit = function
            break
    assert isinstance(submit, dict)
    parameters = submit["parameters"]
    assert isinstance(parameters, dict)
    properties = parameters["properties"]
    assert isinstance(properties, dict)
    reject_invariant = properties["reject_invariant"]
    assert isinstance(reject_invariant, dict)
    reject_invariant["enum"] = [
        item.value for item in source_review_invariants_for_policy(policy_version)
    ]
    return tools


@dataclass(frozen=True)
class _Verdict:
    """A model decision that has not yet been checked against the archive."""

    decision: str
    reason: str
    reject_invariant: str | None
    clear_clause: str | None
    citations: tuple[tuple[str, int], ...]


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= 10_000_000:
        return value
    return None


def _http_status(error: BaseException) -> int | None:
    if not isinstance(error, httpx.HTTPStatusError):
        return None
    code = error.response.status_code
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    if 100 <= code <= 599:
        return code
    return None


def _failure_stage(error: BaseException) -> _RunStage:
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "completion"
    if isinstance(error, httpx.HTTPStatusError):
        return "response"
    if isinstance(error, (httpx.HTTPError, OSError)):
        return "unavailable"
    if isinstance(error, json.JSONDecodeError):
        return "response"
    if isinstance(error, ValueError):
        message = str(error)
        if "lease budget" in message:
            return "lease"
        if "step budget" in message:
            return "step-budget"
    return "response"


def _failure_code(error: BaseException) -> str:
    """Classify only known local failure shapes; never persist exception text."""
    if isinstance(error, (CompletionWireTooLarge, CompletionToolTooLarge)):
        # Keep the old failure_code wire value so an older Platform deployment
        # can still validate this observation during a rolling release.
        return "response-too-large"
    if isinstance(error, ProviderStreamError):
        return "provider-stream-error"
    if isinstance(error, ProviderBodyError):
        return "provider-body-error"
    if isinstance(error, IncompleteStreamError):
        return "stream-incomplete"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "completion-timeout"
    if isinstance(error, httpx.HTTPStatusError):
        return "provider-http-error"
    if isinstance(error, (httpx.HTTPError, OSError)):
        return "transport-error"
    if isinstance(error, json.JSONDecodeError):
        return "response-json-invalid"
    if isinstance(error, ValueError):
        message = str(error)
        if message == "adjudicator model body was unusable":
            return "provider-body-error"
        if message == "adjudicator stream ended without a tool call":
            return "stream-no-tool-call"
        if message.startswith("adjudicator stream "):
            return "stream-invalid"
        if message.startswith("adjudicator exceeded lease budget"):
            return "lease-budget"
        if message.startswith("adjudicator exceeded step budget"):
            return "step-budget"
        if message.startswith("adjudicator decision ") or message.startswith(
            "adjudicator reason "
        ):
            return "verdict-invalid"
        if message.startswith("adjudicator arguments ") or message.startswith(
            ("adjudicator tool call ", "adjudicator function call ")
        ):
            return "tool-call-invalid"
    return "response-invalid"


def _observe_completion(payload: object) -> None:
    """Record token counts and whether a final tool call was present.

    Metadata only. Model text, tool arguments, and prompts are not stored.
    """
    _observe_upstream(payload)
    trace = _run_trace.get()
    if trace is None or not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = _token_count(usage.get("prompt_tokens"))
        completion_tokens = _token_count(usage.get("completion_tokens"))
        if prompt_tokens is not None:
            trace.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            trace.completion_tokens = completion_tokens
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        trace.final_tool_call_returned = False
        return
    trace.final_tool_call_returned = any(
        isinstance(call, dict)
        and isinstance(call.get("function"), dict)
        and call["function"].get("name") == "submit_adjudication"
        for call in calls
    )


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


def _has_unreviewed_lead(
    notes: Sequence[Mapping[str, object]],
    finding: Mapping[str, object] | None,
    read_locations: set[tuple[str, int]],
) -> bool:
    """A decision-only court cannot clear a concern it was never shown."""
    leads: list[Mapping[str, object]] = [
        note for note in notes if note.get("kind") == "concern"
    ]
    if isinstance(finding, Mapping):
        evidence = finding.get("evidence")
        if isinstance(evidence, list):
            leads.extend(item for item in evidence if isinstance(item, Mapping))
    for lead in leads:
        path = lead.get("path")
        line = lead.get("line")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(line, int)
            or isinstance(line, bool)
            or (path.removeprefix("./"), line) not in read_locations
        ):
            return True
    return False


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
        inference_provider: str = "openrouter",
    ) -> None:
        self._api_key_file = api_key_file
        self._base_url = base_url.rstrip("/")
        self._inference_provider = inference_provider
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
            return _escalate(
                "adjudicator-unavailable",
                "Automated adjudication was unavailable on this node; "
                "held for retry or operator review",
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
        unreviewed_concerns = False
        if not notes and error_code in _BUDGET_TERMINATED_REVIEW_CODES:
            # An upstream review consumed its discovery budget without
            # recording evidence. There is nothing for the court to decide;
            # settle rather than spend its reserve rediscovering the archive.
            return _escalate(
                "adjudicator-no-evidence",
                "Automated adjudication received no retained source evidence; "
                "held for operator review",
                model=self._model,
                notes=note_count,
                policy_version=policy_version,
            )
        if decision_only:
            preloaded_evidence, preloaded_reads = _preload_ledger_evidence(
                repository, notes
            )
            # The ledger can retain 48 notes but the one-turn court preloads
            # only 16 distinct locations. A later concern must not disappear
            # behind that bound while an earlier excerpt supports a CLEAR.
            unreviewed_concerns = _has_unreviewed_lead(notes, finding, preloaded_reads)
            if not preloaded_evidence:
                # The upstream layers retained a ledger but no usable source
                # evidence. There is nothing for a court to decide; do not
                # burn its reserve rediscovering the archive. This is still the
                # terminal no-proven-breach adjudication, not a retry.
                return _escalate(
                    "adjudicator-no-evidence",
                    "Automated adjudication received no retained source evidence; "
                    "held for operator review",
                    model=self._model,
                    notes=note_count,
                    policy_version=policy_version,
                )
        trace = _RunTrace(started=asyncio.get_running_loop().time())
        token = _run_trace.set(trace)
        try:
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
                # Fixed class, stage, and subtype only. Exception text can
                # echo a prompt or provider body, so never persist it.
                logger.warning(
                    "adjudication failed model=%s upstream=%s cause=%s "
                    "stage=%s code=%s",
                    self._model,
                    trace.upstream,
                    type(error).__name__,
                    _failure_stage(error),
                    _failure_code(error),
                )
                result = _escalate(
                    "adjudicator-failed",
                    "Automated adjudication did not complete; held for operator review",
                    model=self._model,
                    notes=note_count,
                    policy_version=policy_version,
                )
                return self._with_diagnostic(
                    result,
                    self._failure_diagnostic(
                        trace,
                        error,
                        escalation_code="adjudicator-failed",
                    ),
                )
            return self._certify(
                verdict,
                repository=repository,
                read_locations=read_locations,
                notes=note_count,
                policy_version=policy_version,
                unreviewed_concerns=unreviewed_concerns,
            )
        finally:
            _run_trace.reset(token)

    def _with_diagnostic(
        self,
        result: SourceReviewAdjudication,
        diagnostic: AdjudicationRunDiagnostic | None,
    ) -> SourceReviewAdjudication:
        if diagnostic is None:
            return result
        try:
            return result.model_copy(update={"run_diagnostic": diagnostic})
        except ValidationError:
            logger.warning(
                "adjudication diagnostic was not attachable model=%s",
                self._model,
            )
            return result

    def _failure_diagnostic(
        self,
        trace: _RunTrace,
        error: BaseException,
        *,
        escalation_code: str,
    ) -> AdjudicationRunDiagnostic | None:
        name = type(error).__name__
        error_class = name if _ERROR_CLASS_RE.fullmatch(name) else None
        http_status = _http_status(error)
        if http_status is None:
            http_status = trace.http_status
        elapsed_ms = int((asyncio.get_running_loop().time() - trace.started) * 1000)
        elapsed_ms = min(max(elapsed_ms, 0), 3_600_000)
        model = self._model if _MODEL_RE.fullmatch(self._model) else None
        provider = (
            self._inference_provider
            if _PROVIDER_RE.fullmatch(self._inference_provider)
            else None
        )
        try:
            return AdjudicationRunDiagnostic(
                error_class=error_class,
                failure_code=_failure_code(error),
                escalation_code=escalation_code,
                timeout_stage=_failure_stage(error),
                http_status=http_status,
                elapsed_ms=elapsed_ms,
                prompt_tokens=trace.prompt_tokens,
                completion_tokens=trace.completion_tokens,
                final_tool_call_returned=trace.final_tool_call_returned,
                model=model,
                provider=provider,
                upstream=trace.upstream,
                response_bound_kind=(
                    "wire"
                    if isinstance(error, CompletionWireTooLarge)
                    else "tool"
                    if isinstance(error, CompletionToolTooLarge)
                    else None
                ),
            )
        except ValidationError:
            logger.warning(
                "adjudication diagnostic dropped model=%s class=%s",
                self._model,
                error_class,
            )
            return None

    def _certify(
        self,
        verdict: _Verdict,
        *,
        repository: TarSourceRepository,
        read_locations: set[tuple[str, int]],
        notes: int,
        policy_version: int,
        unreviewed_concerns: bool = False,
    ) -> SourceReviewAdjudication:
        """Refuse any decision the host cannot verify against the archive.

        This is the whole safety argument for using a small model here. The
        decision itself is cheap to check: the citations have to exist, have to
        be code, and have to be locations this adjudicator actually opened.
        """
        if verdict.decision == "escalate":
            return _escalate(
                "adjudicator-evidence-incomplete",
                "Automated adjudication could not complete mandatory verification; "
                "held for operator review",
                model=self._model,
                notes=notes,
                policy_version=policy_version,
            )
        if not verdict.citations:
            return _escalate(
                "uncited-decision",
                "Automated adjudication cited no source; held for operator review",
                model=self._model,
                notes=notes,
                policy_version=policy_version,
            )
        if verdict.decision == "clear" and unreviewed_concerns:
            return _escalate(
                "adjudicator-evidence-incomplete",
                "Automated adjudication did not receive every retained source "
                "lead; held for operator review",
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
        permitted_invariants = {
            item.value for item in source_review_invariants_for_policy(policy_version)
        }
        if (
            verdict.reject_invariant is not None
            and verdict.reject_invariant not in permitted_invariants
        ):
            return _escalate(
                "verdict-contract-failed",
                "Automated adjudication named an invariant outside the applied "
                "policy; held for operator review",
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
            "call submit_adjudication for a complete decision, or "
            "request_operator_review if evidence remains incomplete."
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
        tools = _adjudicator_tools_for_policy(
            policy_version, decision_only=decision_only
        )
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
                # All calls in one assistant message are chosen before any
                # tool result is returned. A verdict in that same batch must
                # not gain credit for a sibling read_file/search result that
                # the model had not seen when it made the decision. Nor may a
                # duplicate verdict silently settle by whichever came first.
                if len(tool_calls) != 1 and (
                    decision_only
                    or any(
                        isinstance(call, dict)
                        and isinstance(call.get("function"), dict)
                        and call["function"].get("name")
                        in {"submit_adjudication", "request_operator_review"}
                        for call in tool_calls
                    )
                ):
                    raise ValueError(
                        "adjudicator terminal decision must be the sole call "
                        "in its turn"
                    )
                for call in tool_calls:
                    call_id, name, arguments = _tool_call(call)
                    if name == "submit_adjudication":
                        return _verdict_from(arguments), read_locations
                    if name == "request_operator_review" and policy_version >= 13:
                        reason = arguments.get("reason")
                        if not isinstance(reason, str) or not reason.strip():
                            raise ValueError(
                                "adjudicator operator review has no reason"
                            )
                        return (
                            _Verdict("escalate", reason.strip(), None, None, ()),
                            read_locations,
                        )
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
            # The court needs the final tool call, but a buffered response can
            # hide a stalled provider for the entire request deadline. SSE
            # exposes progress and gives each read a separate idle bound.
            "stream": True,
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
                # The default is price-weighted. L4 has a short, finite lease;
                # rank eligible endpoints by output speed before fallback.
                "sort": "throughput",
                "data_collection": "deny",
                "require_parameters": True,
            },
        }
        effective_timeout = min(
            timeout if timeout is not None else self._timeout_seconds,
            _MAX_COMPLETION_REQUEST_SECONDS,
        )
        for attempt in range(_MAX_COMPLETION_REQUEST_ATTEMPTS):
            _clear_request_trace()
            try:
                async with asyncio.timeout(effective_timeout):
                    async with client.stream(
                        "POST",
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            **review_gateway_headers(self._inference_provider),
                        },
                        json=request,
                        timeout=httpx.Timeout(
                            effective_timeout,
                            read=min(effective_timeout, _MAX_COMPLETION_IDLE_SECONDS),
                        ),
                    ) as response:
                        # Older OpenAI-compatible gateways may reject SSE
                        # outright. Preserve the previously working buffered
                        # path once, without interpreting any response body as
                        # a verdict or relaxing the request/lease deadlines.
                        if (
                            attempt == 0
                            and request["stream"] is True
                            and response.status_code in {400, 422}
                        ):
                            request["stream"] = False
                            continue
                        if response.status_code >= 400:
                            trace = _run_trace.get()
                            if trace is not None and 100 <= response.status_code <= 599:
                                trace.http_status = response.status_code
                            response.raise_for_status()
                        payload = await _completion_stream_payload(response)
                        _observe_upstream(payload)
                        if _retryable_model_error_type(payload) is not None:
                            raise ProviderBodyError(
                                "adjudicator model body was unusable"
                            )
            except (
                TimeoutError,
                httpx.TransportError,
                IncompleteStreamError,
                ProviderStreamError,
                ProviderBodyError,
            ):
                if attempt + 1 == _MAX_COMPLETION_REQUEST_ATTEMPTS:
                    raise
                logger.warning(
                    "adjudicator completion transport failed; retrying once model=%s",
                    self._model,
                )
                continue
            break
        return _assistant_message(payload)


async def _completion_stream_payload(response: httpx.Response) -> object:
    """Assemble one bounded OpenAI-compatible streamed tool-call response.

    A few compatible gateways return a regular JSON response despite
    ``stream=true``; accept that complete response too. Neither partial SSE
    output nor a truncated JSON body can become a verdict.
    """
    if "text/event-stream" not in response.headers.get("content-type", "").lower():
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_COMPLETION_STREAM_BYTES:
                raise CompletionWireTooLarge(
                    "adjudicator completion exceeded response bound"
                )
        payload = json.loads(body)
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    buffered_calls = message.get("tool_calls")
                    if (
                        buffered_calls is not None
                        and len(json.dumps(buffered_calls).encode("utf-8"))
                        > _MAX_COMPLETION_RESPONSE_BYTES
                    ):
                        raise CompletionToolTooLarge(
                            "adjudicator completion exceeded response bound"
                        )
        return payload

    calls: dict[int, dict[str, object]] = {}
    usage: object = None
    model: object = None
    finish_reason: object = None
    data_lines: list[str] = []
    total_bytes = 0
    retained_bytes = 0
    done = False

    def consume_event() -> bool:
        nonlocal usage, model, finish_reason, retained_bytes
        if not data_lines:
            return False
        data = "\n".join(data_lines)
        data_lines.clear()
        if data == "[DONE]":
            return True
        event = json.loads(data)
        if not isinstance(event, dict):
            raise ValueError("adjudicator stream event is not an object")
        _observe_upstream(event)
        if event.get("error"):
            raise ProviderStreamError("adjudicator stream returned a provider error")
        model = event.get("model") or model
        usage = event.get("usage") or usage
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("adjudicator stream choice is invalid")
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise ValueError("adjudicator stream delta is invalid")
        for piece in delta.get("tool_calls") or []:
            if not isinstance(piece, dict) or not isinstance(piece.get("index"), int):
                raise ValueError("adjudicator stream tool index is invalid")
            index = piece["index"]
            if index < 0 or index >= 32:
                raise ValueError("adjudicator stream tool index exceeded bound")
            call = calls.setdefault(index, {"type": "function", "function": {}})
            for key in ("id", "type"):
                if key in piece:
                    if not isinstance(piece[key], str):
                        raise ValueError("adjudicator stream tool field is invalid")
                    # These are replacement fields, unlike function argument
                    # fragments below. Some compatible gateways repeat the
                    # call ID in every delta. Bound stored tool data, not the
                    # sum of IDs that were overwritten and discarded.
                    previous = call.get(key)
                    if isinstance(previous, str):
                        retained_bytes -= len(previous.encode("utf-8"))
                    retained_bytes += len(piece[key].encode("utf-8"))
                    if retained_bytes > _MAX_COMPLETION_RESPONSE_BYTES:
                        raise CompletionToolTooLarge(
                            "adjudicator completion exceeded response bound"
                        )
                    call[key] = piece[key]
            fragment = piece.get("function") or {}
            if not isinstance(fragment, dict):
                raise ValueError("adjudicator stream function is invalid")
            function = call["function"]
            if not isinstance(function, dict):
                raise ValueError("adjudicator stream function is invalid")
            for key in ("name", "arguments"):
                value = fragment.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("adjudicator stream function field is invalid")
                    retained_bytes += len(value.encode("utf-8"))
                    if retained_bytes > _MAX_COMPLETION_RESPONSE_BYTES:
                        raise CompletionToolTooLarge(
                            "adjudicator completion exceeded response bound"
                        )
                    function[key] = str(function.get(key) or "") + value
        return False

    async for line in response.aiter_lines():
        total_bytes += len(line.encode("utf-8")) + 1
        if total_bytes > _MAX_COMPLETION_STREAM_BYTES:
            raise CompletionWireTooLarge(
                "adjudicator completion exceeded response bound"
            )
        if not line:
            if consume_event():
                done = True
                break
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if not done and consume_event():
        done = True
    if not done:
        raise IncompleteStreamError("adjudicator stream ended before [DONE]")
    if not calls:
        _observe_completion(
            {"usage": usage, "choices": [{"message": {"tool_calls": []}}]}
        )
        raise ValueError("adjudicator stream ended without a tool call")
    return {
        "model": model,
        "usage": usage,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [calls[index] for index in sorted(calls)],
                },
            }
        ],
    }


def _assistant_message(payload: object) -> dict[str, object]:
    _observe_completion(payload)
    # Metadata only: never log private source, prompts, model text or arguments.
    if isinstance(payload, dict):
        choices = payload.get("choices")
        choice = (
            choices[0]
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else {}
        )
        message = choice.get("message") or {}
        if isinstance(message, dict) and not message.get("tool_calls"):
            usage = payload.get("usage") or {}
            logger.warning(
                "model response without tools model=%s finish=%s "
                "content_chars=%s prompt_tokens=%s completion_tokens=%s",
                payload.get("model"),
                choice.get("finish_reason"),
                len(str(message.get("content") or "")),
                usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                usage.get("completion_tokens") if isinstance(usage, dict) else None,
            )
    if not isinstance(payload, dict):
        raise ValueError("adjudicator response is not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("adjudicator response has no choice")
    finish_reason = choices[0].get("finish_reason")
    if finish_reason is not None and finish_reason not in ("tool_calls", "stop"):
        raise ValueError("adjudicator completion was not terminal")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("adjudicator response has no message")
    if message.get("tool_calls"):
        # Buffered gateways may include unused text next to the tool call.
        # Do not carry that text into the next request's conversation history.
        return {**message, "content": None}
    return message


def _tool_call(call: object) -> tuple[str, str, dict[str, object]]:
    if not isinstance(call, dict) or not isinstance(call.get("id"), str):
        raise ValueError("adjudicator tool call is invalid")
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ValueError("adjudicator function call is invalid")
    raw = function.get("arguments")
    # Keep the arguments as a string. A parsed object is a contract miss, and
    # its contents are model text: record the failure class, do not store it.
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
    if len(reason) > 8000:
        raise ValueError("adjudicator reason exceeds 8000 characters")
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
        reason=reason.strip(),
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
        inference_provider=str(
            getattr(config, "review_inference_provider", "openrouter")
        ),
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
