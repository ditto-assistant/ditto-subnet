#!/usr/bin/env python3
"""Replay the public Bench v13 gate rules against a local DittoBench rehearsal.

`local-rehearsal.py --gates` calls :func:`evaluate_run` after a run and prints
per-case notes; this module is also a standalone CLI so the same replay can be
re-run offline against the kept pass-off artifacts (the salt=0 public dataset
artifact, the run transcript, the harness completion log, and the report).

Everything here is a deterministic, model-free port of the PUBLISHED rules
(services/dittobench-api/PROTOCOL.md "bench_version 13"), so what a miner sees
locally is the rule the validator states, not a guess:

* ``restraint_without_offer`` / ``expected_tool_not_offered`` /
  ``swallowed_model_call`` — the catalog-present gate with its
  semantic-preloading safe harbor (top-3 of the published TF-IDF embedding).
* ``served_text_not_model_emitted`` — the claim-span provenance gate: the
  graded CLAIM SPAN (the accepted alternative wholly present in the served
  slot or prose, under the published ``NormalizeSpan`` normaliser) must be
  contained in the union of every model completion's value tokens (message
  text, tool-call arguments, structured output). No alternative present means
  ``claim_not_applicable``: the gate fails open, never guesses.
* ``slot_not_in_prose`` — a populated ``answer`` slot whose value has no
  equivalent in ``final_text`` (numbers compare canonically and across the
  minor/major unit alternative, so ``411067`` beside ``$4,110.67`` passes).
* ``answer_in_prompt`` — the causal gate with the records exemption: every
  claim token was authored by the harness before any completion produced it
  and none of them is covered by a ``/seed`` record, a retrieved-memory context
  message, a delivered tool result, or the case's own question.
* ``twin_concordant`` / ``counterfactual_insensitive`` — the twin/pair
  post-pass: identical decision class across a decision/as-of twin group, or a
  counterfactual member answered like its base. Metamorphic relations ride on
  the artifact (``v10_provenance.relation``); decision/as-of twin relations
  are grader-only and reach the replay solely through the scorer report's
  ``per_case[].relation``, so those notes need ``--report``.

Every gate is SHADOW in v13.0: notes are printed and a "gate-induced loss
(shadow)" is computed, no local score moves. The local instrument sees the
kit's own completion log where the validator sees its relay record; the rule
is the same, the evidence source differs.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# The kit's own instruments run against the public (salt=0) surface pass; a
# scored run uses the validator's private salt, which changes surface text but
# never a rule below.
SURFACE_SALT = 0

SAFE_HARBOR_TOP_K = 3
MEMORY_TOOLS = frozenset(
    {
        "search_memories",
        "search_subjects",
        "fetch_memories",
        "search_memories_in_subjects",
    }
)
# No-tool categories whose prompt NAMES a tool cue while negating it: the
# non-empty-catalog safe harbor does not apply, the retained set must hold the
# semantic top-k.
TEMPTING_CLASS_CATEGORIES = frozenset({"negation_no_tool"})

FINDING_RESTRAINT_WITHOUT_OFFER = "restraint_without_offer"
FINDING_EXPECTED_TOOL_NOT_OFFERED = "expected_tool_not_offered"
FINDING_SWALLOWED_MODEL_CALL = "swallowed_model_call"
FINDING_SAFE_HARBOR = "semantic_preloading_safe_harbor"
FINDING_EVIDENCE_UNAVAILABLE = "catalog_evidence_unavailable"
FINDING_NO_MODEL_COMPLETION = "no_model_completion"
FINDING_CATALOG_ABSENT = "catalog_absent"
FINDING_SERVED_TEXT_NOT_MODEL_EMITTED = "served_text_not_model_emitted"
FINDING_SLOT_NOT_IN_PROSE = "slot_not_in_prose"
FINDING_ANSWER_IN_PROMPT = "answer_in_prompt"
FINDING_TWIN_CONCORDANT = "twin_concordant"
FINDING_COUNTERFACTUAL_INSENSITIVE = "counterfactual_insensitive"
FINDING_PROVENANCE_UNAVAILABLE = "provenance_evidence_unavailable"
FINDING_MEMORY_ONLY_CATALOG = "memory_only_catalog"
FINDING_OFFER_INFERRED_FROM_EXECUTION = "offer_inferred_from_execution"

GATE_FINDINGS = (
    FINDING_RESTRAINT_WITHOUT_OFFER,
    FINDING_EXPECTED_TOOL_NOT_OFFERED,
    FINDING_SWALLOWED_MODEL_CALL,
    FINDING_SERVED_TEXT_NOT_MODEL_EMITTED,
    FINDING_SLOT_NOT_IN_PROSE,
    FINDING_ANSWER_IN_PROMPT,
    FINDING_TWIN_CONCORDANT,
    FINDING_COUNTERFACTUAL_INSENSITIVE,
)

SAFE_HARBOR_NONEMPTY_DECLARATIVE = "nonempty_catalog_on_declarative"
SAFE_HARBOR_SEMANTIC_TOP_K = "retained_semantic_top_k"

DECISION_TWIN_RELATIONS = frozenset({"decision_twin", "as_of_twin"})
METAMORPHIC_BASE = "base"
METAMORPHIC_COUNTERFACTUAL = "causal_counterfactual"
AUDIT_TWIN_PREFIX = "auditxf-"
INJECTION_TWIN_PREFIX = "injtwin-"

# ─── Published semantic embedding (scorer.CatalogSemanticTopK) ───────────────

_STOPWORD_TEXT = """the and for with that this from you your can please one more
    use are was were have has had not but any all into its our out about what
    which when where who how why just like then than too very will would should
    could there here some them they their thing tool tools given return returns
    default"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def catalog_tokens(text: str) -> set[str]:
    """Lowercase, split on every non-ASCII-alphanumeric character, drop short
    tokens and stopwords, stem. Identical to the scorer and the kit."""
    tokens: set[str] = set()
    for raw in re.split(r"[^a-z0-9]+", text.lower()):
        if len(raw) < 3 or raw in _STOPWORDS:
            continue
        tokens.add(_stem(raw))
    return tokens


def semantic_top_k(prompt: str, catalog: list[dict[str, Any]], k: int) -> list[str]:
    """Names of the top-k catalog tools under the published TF-IDF embedding,
    ties broken on name."""
    if k <= 0 or not catalog:
        return []
    docs = [
        catalog_tokens(f"{tool.get('name', '')} {tool.get('description', '')}")
        for tool in catalog
    ]
    df: dict[str, int] = {}
    for doc in docs:
        for token in doc:
            df[token] = df.get(token, 0) + 1
    n = len(catalog)

    def idf(token: str) -> float:
        return math.log(1 + n / (1 + df.get(token, 0)))

    query = catalog_tokens(prompt)
    query_norm = sum(idf(token) ** 2 for token in query)
    ranked: list[tuple[float, str]] = []
    for tool, doc in zip(catalog, docs, strict=True):
        dot = 0.0
        doc_norm = 0.0
        for token in doc:
            weight = idf(token) ** 2
            doc_norm += weight
            if token in query:
                dot += weight
        score = 0.0
        if dot > 0 and doc_norm > 0 and query_norm > 0:
            score = dot / (math.sqrt(doc_norm) * math.sqrt(query_norm))
        ranked.append((score, str(tool.get("name", ""))))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    return [name for _, name in ranked[:k]]


# ─── Provenance normaliser (scoregates/text_provenance.go, verbatim port) ────
#
# Everything in this section is a line-for-line port of the PUBLISHED Go rule
# (services/dittobench-api/internal/scoregates/text_provenance.go and
# causal_dependence.go). The Go side stores value-token HASHES; this port keeps
# the canonical token strings, which is the same set under HashToken. The Go
# vectors (text_provenance_test.go, audit_v13_bank.go) are replayed against this
# module in test_local_rehearsal.py so the two cannot drift.

# RE2 `\d` and `\s` are ASCII-only; spell them out so Python matches Go.
_D = r"[0-9]"
_WS = r"[\t\n\f\r ]"
# numberPattern: optional sign, optional '$', digits with grouping commas,
# optional fractional part.
NUMBER_RE = re.compile(rf"-?\$?{_D}[{_D[1:-1]},]*(?:\.{_D}+)?")
# alnumTokenPattern over already-lowercased text.
ALNUM_RE = re.compile(r"[a-z0-9]+")
# labelPrefixPattern: a leading answer label on a line ("ANSWER:", "Final
# answer -", "A:", "Result:"), optionally wrapped in markdown emphasis.
_LABEL_PREFIX_RE = re.compile(
    rf"(?im)^[{_WS[1:-1]}*_#>\-]*(?:final{_WS}+answer|answer|result|response|output|a){_WS}*[:\-–—]{_WS}*"
)
# listMarkerPattern: markdown bullets and ordered-list ordinals at line start.
_LIST_MARKER_RE = re.compile(rf"(?m)^{_WS}*(?:[-*+•]|{_D}{{1,3}}[.)]){_WS}+")
# Value-token capture bounds (scoregates.MinStringTokenLen / MaxValueTokenLen).
MIN_STRING_TOKEN_LEN = 4
MAX_VALUE_TOKEN_LEN = 64
# Punctuation that carries numeric meaning inside a number; NormalizeSpan keeps
# it and CanonicalNumber decides what it means.
_NUMERIC_PUNCT = frozenset("$.,-")

FINDING_CLAIM_NOT_APPLICABLE = "claim_not_applicable"

# grade.ClaimAlternatives vocabulary (grade.go increasePhrases/decreasePhrases).
_DIRECTION_PHRASES = {
    "increase": (
        "increase",
        "increased",
        "went up",
        "rose",
        "grew",
        "raise",
        "raised",
        "gain",
        "gained",
        "higher",
    ),
    "decrease": (
        "decrease",
        "decreased",
        "went down",
        "fell",
        "dropped",
        "reduced",
        "reduction",
        "lower",
        "lowered",
        "cut",
    ),
}


def canonical_number(raw: str) -> str:
    """Port of ``scoregates.CanonicalNumber``: strip ``$`` and grouping
    commas, drop an insignificant fraction and trailing zeros, drop leading
    zeros, collapse ``-0``. Empty when not a number."""
    value = raw.strip()
    negative = value.startswith("-")
    value = value.removeprefix("-").replace("$", "").replace(",", "")
    if not value:
        return ""
    int_part, _, frac_part = value.partition(".")
    frac_part = frac_part.rstrip("0")
    int_part = int_part.lstrip("0") or "0"
    if any(ch < "0" or ch > "9" for ch in int_part + frac_part):
        return ""
    out = int_part + ("." + frac_part if frac_part else "")
    if negative and out != "0":
        out = "-" + out
    return out


def normalize_span(text: str) -> str:
    """Port of ``scoregates.NormalizeSpan``, the PUBLISHED request-independent
    normaliser both sides of the claim-span gate apply before tokenizing:
    Unicode NFKC, label and list-marker stripping, letters lowercased, digits
    kept, ``$ . , -`` kept (numeric meaning), every other rune folded to one
    separator, whitespace collapsed. Deterministic, total, idempotent."""
    folded = unicodedata.normalize("NFKC", text)
    folded = _LABEL_PREFIX_RE.sub("", folded)
    folded = _LIST_MARKER_RE.sub("", folded)
    out: list[str] = []
    prev_space = True
    for ch in folded:
        if ch.isalpha() or unicodedata.category(ch) == "Nd":
            out.append(ch.lower())
            prev_space = False
        elif ch in _NUMERIC_PUNCT:
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


# The slot rule (`slot_not_in_prose`) folds text through the same normaliser.
fold_text = normalize_span


def value_tokens(text: str) -> set[str]:
    """Port of ``scoregates.SpanTokens`` (``ValueTokenHashes`` over
    ``NormalizeSpan``): every canonical number plus every lowercase
    alphanumeric token of at least ``MIN_STRING_TOKEN_LEN`` that is not a pure
    digit run, each at most ``MAX_VALUE_TOKEN_LEN`` long."""
    normalized = normalize_span(text)
    tokens: set[str] = set()
    for match in NUMBER_RE.findall(normalized):
        canonical = canonical_number(match)
        if canonical and len(canonical) <= MAX_VALUE_TOKEN_LEN:
            tokens.add(canonical)
    for token in ALNUM_RE.findall(normalized.lower()):
        if (
            MIN_STRING_TOKEN_LEN <= len(token) <= MAX_VALUE_TOKEN_LEN
            and not token.isdigit()
        ):
            tokens.add(token)
    return tokens


def _grade_normalize(value: str) -> str:
    """``grade.Normalize``: trim, lowercase, strip edge punctuation, collapse."""
    folded = value.strip().lower().strip("\"'.,!?;:")
    return " ".join(folded.split())


def money_major_form(expected: str) -> str | None:
    """``grade.MoneyMajorForm``: integer minor units -> major-unit decimal."""
    digits = _grade_normalize(expected)
    if not digits or any(ch < "0" or ch > "9" for ch in digits):
        return None
    cents = int(digits)
    return f"{cents // 100}.{cents % 100:02d}"


def direction_phrases(expected: str) -> list[str]:
    """``grade.DirectionPhrases``: the grader's own acceptance table."""
    wanted = _grade_normalize(expected)
    for phrases in _DIRECTION_PHRASES.values():
        if wanted in phrases:
            return list(phrases)
    return []


def claim_alternatives(case: dict[str, Any]) -> list[str]:
    """Port of ``grade.ClaimAlternatives``: the accepted canonical surface
    forms of the expected answer, by answer kind. Kinds without a checkable
    value (decline, acknowledge, chitchat, clarify, ...) have none."""
    kind = str(case.get("answer_kind") or "value")
    out: list[str] = []

    def add(value: Any) -> None:
        text = str(value).strip() if value is not None else ""
        if text:
            out.append(text)

    if kind == "value":
        add(case.get("expected_answer"))
        for alt in _as_list(case.get("accept_any")):
            add(alt)
    elif kind == "number":
        add(case.get("expected_answer"))
    elif kind == "money":
        add(money_major_form(str(case.get("expected_answer") or "")))
    elif kind == "direction":
        for phrase in direction_phrases(str(case.get("expected_answer") or "")):
            add(phrase)
    elif kind in ("list", "ordered_list"):
        accept = _as_list(case.get("answer_item_accept_any"))
        for index, item in enumerate(_as_list(case.get("answer_items"))):
            add(item)
            if index < len(accept):
                for alt in _as_list(accept[index]):
                    add(alt)
    return out


def served_claim_tokens(
    served_text: str, alternatives: Iterable[str]
) -> tuple[set[str], bool]:
    """Port of ``scoregates.ServedClaimTokens``: the claim tokens are the union
    of the token sets of every accepted alternative WHOLLY present in the
    served span. ``ok`` is False when no alternative is present or every present
    one has an empty token set: the gate then has no claim span and MUST fail
    open (``claim_not_applicable``) rather than guess."""
    served = value_tokens(served_text)
    claim: set[str] = set()
    for alternative in alternatives:
        alt_tokens = value_tokens(alternative)
        if not alt_tokens or not alt_tokens <= served:
            continue
        claim |= alt_tokens
    if not claim:
        return set(), False
    return claim, True


def text_provenance(claim: set[str], completions: set[str]) -> bool:
    """``scoregates.TextProvenance``: the served claim tokens must be a subset
    of the union of the case's completion tokens. An empty claim passes."""
    if not claim:
        return True
    return claim <= completions


def causal_dependence(claim: set[str], residual_harness_first: set[str]) -> bool:
    """``scoregates.CausalDependence``: every claim token was authored by the
    harness before any completion produced it. An empty claim never fires."""
    if not claim:
        return False
    return claim <= residual_harness_first


def residual_harness_tokens(harness_first: set[str], *exemptions: set[str]) -> set[str]:
    """``scoregates.ResidualHarnessTokens``: harness-first tokens minus every
    exemption (delivered records, tool results, the case's own question)."""
    residual = set(harness_first)
    for exempt in exemptions:
        residual -= exempt
    return residual


class ClaimSpanLedger:
    """Port of ``scoregates.ClaimSpanLedger``: the per-case token record both
    gates read. Ordering matters for the causal gate: a harness-authored token
    is harness-first only when no EARLIER completion of the same case already
    produced it, so a value the model derived and the harness later re-injected
    is model-derived, not laundered."""

    def __init__(self) -> None:
        self.completion: set[str] = set()
        self.harness_first: set[str] = set()
        self.tool_result: set[str] = set()
        self.completions = 0
        self.tool_results = 0

    def record_call(self, harness: Iterable[str], completion: Iterable[str]) -> None:
        for span in harness:
            self.harness_first |= value_tokens(span) - self.completion
        for span in completion:
            self.completion |= value_tokens(span)
        self.completions += 1

    def record_tool_result(self, result: str) -> None:
        self.tool_result |= value_tokens(result)
        self.tool_results += 1


def evaluate_claim(
    served: str,
    alternatives: Iterable[str],
    ledger: ClaimSpanLedger,
    *exemptions: set[str],
) -> dict[str, Any]:
    """Port of ``scoregates.EvaluateClaim``: both gates for one credited claim.
    ``applicable`` is False when the served span carries no checkable claim."""
    claim, ok = served_claim_tokens(served, alternatives)
    if not ok:
        return {
            "applicable": False,
            "claim_tokens": 0,
            "model_emitted": True,
            "answer_in_prompt": False,
        }
    residual = residual_harness_tokens(
        ledger.harness_first, ledger.tool_result, *exemptions
    )
    return {
        "applicable": True,
        "claim_tokens": len(claim),
        "model_emitted": text_provenance(claim, ledger.completion),
        "answer_in_prompt": causal_dependence(claim, residual),
    }


def _decimal(canonical: str) -> Decimal | None:
    try:
        return Decimal(canonical)
    except InvalidOperation:
        return None


def numbers_equivalent(a: str, b: str) -> bool:
    """Canonical equality or the minor/major unit alternative (×100)."""
    ca, cb = canonical_number(a), canonical_number(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    da, db = _decimal(ca), _decimal(cb)
    if da is None or db is None:
        return False
    return da * 100 == db or db * 100 == da


def bounded_contains(text: str, phrase: str) -> bool:
    """Whole-token containment of a normalised phrase in normalised text."""
    text_f, phrase_f = fold_text(text), fold_text(phrase)
    if not phrase_f:
        return False
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(phrase_f)}(?![a-z0-9])", text_f)
        is not None
    )


def slot_in_prose(slot: str, final_text: str) -> bool:
    """The grader's ``slot_not_in_prose`` rule: the ``answer`` slot must have an
    equivalent value asserted in the prose — bounded containment of the
    normalised slot, or every slot number having a canonical or unit-alternative
    match among the prose numbers."""
    if not slot.strip():
        return True
    if bounded_contains(final_text, slot):
        return True
    slot_numbers = [canonical_number(m) for m in NUMBER_RE.findall(fold_text(slot))]
    slot_numbers = [n for n in slot_numbers if n]
    if not slot_numbers:
        return False
    prose_numbers = [
        canonical_number(m) for m in NUMBER_RE.findall(fold_text(final_text))
    ]
    return all(
        any(numbers_equivalent(s, p) for p in prose_numbers if p) for s in slot_numbers
    )


# ─── Catalog-present gate (scorer.EvaluateCatalogGate) ───────────────────────


def evaluate_catalog_gate(
    *,
    category: str,
    prompt: str,
    expected_tools: list[str],
    catalog: list[dict[str, Any]],
    completions_total: int | None,
    tools_offered: list[str],
    model_emitted_tool_calls: list[str],
    observed_tool_calls: list[str],
    execution_proven: bool = False,
) -> dict[str, Any]:
    """Pure port of ``scorer.EvaluateCatalogGate`` for one tool case.
    ``execution_proven`` reports whether every observed execution is proven
    model-emitted (matched v10 provenance on the validator; the kit's own
    completion log locally), which lets an executed expected tool waive the
    expected-tool-not-offered rule (``offer_inferred_from_execution``)."""
    verdict: dict[str, Any] = {
        "settled": False,
        "findings": [],
        "safe_harbor": "",
        "zero": False,
        "top_k": [],
    }
    if completions_total is None:
        verdict["findings"].append(FINDING_EVIDENCE_UNAVAILABLE)
        return verdict
    verdict["settled"] = True
    # tools_offered is the CHOOSABLE union; a catalog of memory tools alone
    # leaves nothing to act on, so the model was never in a position to act.
    offered = set(tools_offered)
    catalog_present = bool(offered)
    actionable = any(name not in MEMORY_TOOLS for name in offered)
    no_completion = completions_total == 0
    catalog_absent = not no_completion and not catalog_present
    top_k = semantic_top_k(prompt, catalog, SAFE_HARBOR_TOP_K)
    verdict["top_k"] = top_k
    retained_top_k = bool(top_k) and all(name in offered for name in top_k)
    if no_completion:
        verdict["findings"].append(FINDING_NO_MODEL_COMPLETION)
    if catalog_absent:
        verdict["findings"].append(FINDING_CATALOG_ABSENT)
    if catalog_present and not actionable:
        verdict["findings"].append(FINDING_MEMORY_ONLY_CATALOG)

    if not expected_tools:
        executed: dict[str, int] = {}
        for name in observed_tool_calls:
            executed[name] = executed.get(name, 0) + 1
        pending: dict[str, int] = {}
        for name in model_emitted_tool_calls:
            if name in MEMORY_TOOLS:
                continue
            pending[name] = pending.get(name, 0) + 1
        if any(count > executed.get(name, 0) for name, count in pending.items()):
            verdict["findings"].append(FINDING_SWALLOWED_MODEL_CALL)
            verdict["zero"] = True
        tempting = category in TEMPTING_CLASS_CATEGORIES
        if retained_top_k:
            verdict["safe_harbor"] = SAFE_HARBOR_SEMANTIC_TOP_K
        elif actionable and not tempting:
            verdict["safe_harbor"] = SAFE_HARBOR_NONEMPTY_DECLARATIVE
        else:
            verdict["findings"].append(FINDING_RESTRAINT_WITHOUT_OFFER)
            verdict["zero"] = True
        if verdict["safe_harbor"]:
            verdict["findings"].append(FINDING_SAFE_HARBOR)
        return verdict

    # An expected non-memory tool never offered cannot have been the model's
    # choice -- unless the validator executed that very tool under proven
    # model emission, in which case the offer is inferred from the execution.
    executed_names = set(observed_tool_calls) if execution_proven else set()
    missing: list[str] = []
    inferred = False
    for name in expected_tools:
        if name in MEMORY_TOOLS or name in offered:
            continue
        if name in executed_names:
            inferred = True
            continue
        missing.append(name)
    if inferred:
        verdict["findings"].append(FINDING_OFFER_INFERRED_FROM_EXECUTION)
    if not missing:
        return verdict
    missing.sort()
    verdict["findings"].append(FINDING_EXPECTED_TOOL_NOT_OFFERED)
    if retained_top_k:
        verdict["safe_harbor"] = SAFE_HARBOR_SEMANTIC_TOP_K
        verdict["findings"].append(FINDING_SAFE_HARBOR)
        return verdict
    verdict["zero"] = True
    return verdict


# ─── Run replay ──────────────────────────────────────────────────────────────


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def wire_to_internal_case_ids(projection: dict[str, Any] | None) -> dict[str, str]:
    """The private V9 projection manifest maps the opaque wire case ids the
    harness saw back to the canonical ids the transcript and report carry."""
    mapping: dict[str, str] = {}
    for entry in _as_list((projection or {}).get("cases")):
        if isinstance(entry, dict) and entry.get("wire") and entry.get("internal"):
            mapping[str(entry["wire"])] = str(entry["internal"])
    return mapping


def _completion_texts(entry: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for completion in _as_list(entry.get("completions")):
        if not isinstance(completion, dict):
            continue
        if completion.get("text"):
            texts.append(str(completion["text"]))
        for call in _as_list(completion.get("tool_calls")):
            if isinstance(call, dict) and call.get("args") is not None:
                texts.append(json.dumps(call["args"], sort_keys=True))
    return texts


def _model_emitted(entry: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for completion in _as_list(entry.get("completions")):
        if not isinstance(completion, dict):
            continue
        for call in _as_list(completion.get("tool_calls")):
            if isinstance(call, dict) and call.get("name"):
                names.append(str(call["name"]))
    return names


def _known_values(case: dict[str, Any]) -> list[str]:
    """Every accepted surface form of the case's answer plus its distractors,
    for the decision-class fingerprint of the twin post-pass."""
    forms = claim_alternatives(case)
    if case.get("expected_answer"):
        forms.append(str(case["expected_answer"]))
    forms.extend(str(v) for v in _as_list(case.get("distractor_answers")))
    return forms


def _seed_record_texts(dataset: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for wave in _as_list(dataset.get("memory_waves")):
        if not isinstance(wave, dict):
            continue
        for pair in _as_list(wave.get("pairs")):
            if isinstance(pair, dict):
                texts.append(f"{pair.get('prompt', '')}\n{pair.get('response', '')}")
        for subject in _as_list(wave.get("subjects")):
            if isinstance(subject, dict):
                texts.append(
                    f"{subject.get('subject_text', '')}\n{subject.get('description_text', '')}"
                )
    return texts


def _decision_class(response: dict[str, Any], known_values: list[str]) -> str:
    if response.get("abstain") is True:
        return "abstain"
    slot = str(response.get("answer") or "").strip()
    if slot:
        return "answer:" + " ".join(sorted(value_tokens(slot)))
    final_text = str(response.get("final_text") or "")
    asserted = sorted(
        form_token
        for form in known_values
        for form_token in value_tokens(form)
        if form_token in value_tokens(final_text)
    )
    return "answer:" + " ".join(asserted) if asserted else "answer:none"


def _relation(case: dict[str, Any], scored: dict[str, Any] | None) -> str:
    """Metamorphic relations are serialized on the artifact
    (``v10_provenance.relation``); decision/as-of twin relations are
    grader-only (``TwinRelation`` is ``json:"-"``) and reach the replay only
    through the scorer report's ``per_case[].relation``."""
    provenance = case.get("v10_provenance")
    if isinstance(provenance, dict) and provenance.get("relation"):
        return str(provenance["relation"])
    if scored and scored.get("relation"):
        return str(scored["relation"])
    return ""


def _run_catalog(
    log_by_case: dict[str, dict[str, Any]],
    transcript_by_case: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The published catalog the safe harbor is stated against, recovered from
    the run when a case has no completion-log entry of its own: the union (by
    name) of every kit-log catalog, then every relay ``tools_offered`` name."""
    catalog: dict[str, dict[str, Any]] = {}
    for entry in log_by_case.values():
        for tool in _as_list(entry.get("catalog")):
            if isinstance(tool, dict) and tool.get("name"):
                catalog.setdefault(
                    str(tool["name"]),
                    {
                        "name": str(tool["name"]),
                        "description": str(tool.get("description") or ""),
                    },
                )
    for recorded in transcript_by_case.values():
        relay = _as_dict(_as_dict(recorded.get("execution")).get("catalog"))
        for tool in _as_list(relay.get("tools_offered")):
            if isinstance(tool, dict) and tool.get("name"):
                catalog.setdefault(
                    str(tool["name"]), {"name": str(tool["name"]), "description": ""}
                )
    return [catalog[name] for name in sorted(catalog)]


def _execution_proven(
    scored: dict[str, Any] | None,
    observed: list[str],
    model_emitted: list[str],
    evidence_source: str,
) -> bool:
    """Whether every observed execution is proven model-emitted: matched v10
    provenance on the report (``tool_provenance`` complete, nothing unmatched,
    matched == observed), or, when the evidence is the kit's own completion
    log, every executed name appearing among the model-emitted calls."""
    if not observed:
        return False
    provenance = _as_dict((scored or {}).get("tool_provenance"))
    if provenance:
        return (
            bool(provenance.get("complete"))
            and int(provenance.get("unmatched") or 0) == 0
            and int(provenance.get("matched") or 0) == len(observed)
        )
    if evidence_source == "harness_completion_log":
        emitted: dict[str, int] = {}
        for name in model_emitted:
            emitted[name] = emitted.get(name, 0) + 1
        executed: dict[str, int] = {}
        for name in observed:
            executed[name] = executed.get(name, 0) + 1
        return all(emitted.get(name, 0) >= count for name, count in executed.items())
    return False


def evaluate_run(
    dataset: dict[str, Any],
    transcript: dict[str, Any],
    completions: list[dict[str, Any]],
    report: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay every public v13 gate over one run and return per-case notes plus
    a shadow summary. ``report`` is the scorer's report (for ``per_case`` scores
    and the gate-induced-loss computation); ``projection`` is the private V9
    manifest that maps wire case ids back to canonical ones."""
    wire_map = wire_to_internal_case_ids(projection)
    log_by_case: dict[str, dict[str, Any]] = {}
    for logged in completions:
        if not isinstance(logged, dict) or not logged.get("case_id"):
            continue
        wire_id = str(logged["case_id"])
        log_by_case[wire_map.get(wire_id, wire_id)] = logged
    transcript_by_case: dict[str, dict[str, Any]] = {}
    for case in _as_list(transcript.get("cases")):
        if isinstance(case, dict) and case.get("case_id"):
            transcript_by_case[str(case["case_id"])] = case
    scored_by_case: dict[str, dict[str, Any]] = {}
    for scored in _as_list((report or {}).get("per_case")):
        if isinstance(scored, dict) and scored.get("case_id"):
            scored_by_case[str(scored["case_id"])] = scored

    seed_texts = _seed_record_texts(dataset)
    seed_tokens: set[str] = set()
    for text in seed_texts:
        seed_tokens |= value_tokens(text)
    run_catalog = _run_catalog(log_by_case, transcript_by_case)
    per_case: list[dict[str, Any]] = []
    twin_groups: dict[str, list[dict[str, Any]]] = {}

    for case in _as_list(dataset.get("tool_cases")):
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id", ""))
        entry = log_by_case.get(case_id)
        recorded = transcript_by_case.get(case_id, {})
        execution = _as_dict(recorded.get("execution"))
        relay: dict[str, Any] | None = _as_dict(execution.get("catalog")) or None
        observed = [
            str(call.get("name", ""))
            for call in _as_list(recorded.get("observed"))
            if isinstance(call, dict)
        ]
        if not observed:
            response = _as_dict(recorded.get("response"))
            observed = [
                str(call.get("name", ""))
                for call in _as_list(response.get("tool_calls"))
                if isinstance(call, dict)
            ]
        catalog = [
            {"name": tool.get("name", ""), "description": tool.get("description", "")}
            for tool in _as_list((entry or {}).get("catalog"))
            if isinstance(tool, dict)
        ] or run_catalog
        if (
            relay is not None
            and relay.get("complete")
            and relay.get("completions_total") is not None
        ):
            # The validator's own relay record outranks the kit's log.
            completions_total: int | None = int(relay["completions_total"])
            tools_offered = [
                str(t.get("name", ""))
                for t in _as_list(relay.get("tools_offered"))
                if isinstance(t, dict)
            ]
            model_emitted = [
                str(n) for n in _as_list(relay.get("model_emitted_tool_calls"))
            ]
            evidence_source = "validator_relay"
        elif entry is not None:
            completions_total = len(_as_list(entry.get("completions")))
            tools_offered = [str(n) for n in _as_list(entry.get("tools_offered"))]
            model_emitted = _model_emitted(entry)
            evidence_source = "harness_completion_log"
        else:
            completions_total = None
            tools_offered = []
            model_emitted = []
            evidence_source = "none"
        verdict = evaluate_catalog_gate(
            category=str(case.get("category", "")),
            prompt=str(case.get("prompt", "")),
            expected_tools=[
                str(t.get("name", ""))
                for t in _as_list(case.get("expected_tools"))
                if isinstance(t, dict)
            ],
            catalog=catalog,
            completions_total=completions_total,
            tools_offered=tools_offered,
            model_emitted_tool_calls=model_emitted,
            observed_tool_calls=observed,
            execution_proven=_execution_proven(
                scored_by_case.get(case_id), observed, model_emitted, evidence_source
            ),
        )
        per_case.append(
            {
                "case_id": case_id,
                "kind": "tool",
                "category": case.get("category", ""),
                "evidence_source": evidence_source,
                "findings": verdict["findings"],
                "safe_harbor": verdict["safe_harbor"],
                "semantic_top_k": verdict["top_k"],
                "would_zero": bool(verdict["zero"]),
                "notes": _tool_notes(verdict, evidence_source),
            }
        )

    for case in _as_list(dataset.get("memory_cases")):
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id", ""))
        recorded = transcript_by_case.get(case_id, {})
        response = _as_dict(recorded.get("response"))
        entry = log_by_case.get(case_id)
        scored = scored_by_case.get(case_id)
        slot = str(response.get("answer") or "").strip()
        final_text = str(response.get("final_text") or "")
        # The graded claim span is the text the positive check credited: the
        # slot when populated (authoritative for v9+), the prose otherwise.
        served = slot if slot else final_text
        alternatives = claim_alternatives(case)
        credited = bool(scored.get("correct")) if scored is not None else True
        claim, applicable = served_claim_tokens(served, alternatives)
        findings: list[str] = []
        notes: list[str] = []
        would_zero = False
        if slot and not slot_in_prose(slot, final_text):
            findings.append(FINDING_SLOT_NOT_IN_PROSE)
            notes.append(
                "answer slot has no equivalent value asserted in final_text (slot_not_in_prose)"
            )
            would_zero = True
        if not credited or not applicable:
            # Nothing was credited, or the served span carries no checkable
            # value claim (a decline, a clarifying question, a value below the
            # token floor): the provenance and causal gates fail open.
            findings.append(FINDING_CLAIM_NOT_APPLICABLE)
        elif entry is None:
            findings.append(FINDING_PROVENANCE_UNAVAILABLE)
            notes.append(
                "no completion log for this case; provenance and causal gates not replayed"
            )
        else:
            # The kit composes its system prompt before the first completion
            # and never re-injects a completion, so every harness span is
            # harness-first; tool results the kit received are exempt.
            ledger = ClaimSpanLedger()
            ledger.record_call(
                [str(span) for span in _as_list(entry.get("harness_spans"))], []
            )
            ledger.completions = 0
            for text in _completion_texts(entry):
                ledger.record_call([], [text])
            for result in _as_list(entry.get("tool_results")):
                ledger.record_tool_result(str(result))
            exempt: set[str] = set(seed_tokens)
            for span in _as_list(entry.get("record_spans")):
                exempt |= value_tokens(str(span))
            exempt |= value_tokens(str(entry.get("user_input") or ""))
            exempt |= value_tokens(str(case.get("question") or ""))
            verdict = evaluate_claim(served, alternatives, ledger, exempt)
            if not verdict["model_emitted"]:
                if ledger.completions == 0:
                    findings.append(FINDING_NO_MODEL_COMPLETION)
                findings.append(FINDING_SERVED_TEXT_NOT_MODEL_EMITTED)
                notes.append(
                    "graded claim span absent from every model completion "
                    f"({verdict['claim_tokens']} claim tokens): served_text_not_model_emitted"
                )
                would_zero = True
            if verdict["answer_in_prompt"]:
                findings.append(FINDING_ANSWER_IN_PROMPT)
                notes.append(
                    "graded value present in a harness-authored span not covered by a "
                    "record or tool result: answer_in_prompt"
                )
                would_zero = True
        record = {
            "case_id": case_id,
            "kind": "memory",
            "category": case.get("question_type", ""),
            "twin_group": case.get("twin_group", ""),
            "relation": _relation(case, scored),
            "findings": findings,
            "would_zero": would_zero,
            "notes": notes,
            "_decision_class": _decision_class(response, _known_values(case)),
            "_correct": bool(scored.get("correct")) if scored is not None else None,
        }
        per_case.append(record)
        group = str(case.get("twin_group") or "")
        if group and not group.startswith((AUDIT_TWIN_PREFIX, INJECTION_TWIN_PREFIX)):
            twin_groups.setdefault(group, []).append(record)

    _twin_post_pass(twin_groups)
    for record in per_case:
        record.pop("_decision_class", None)
        record.pop("_correct", None)

    return {
        "schema_version": 1,
        "bench_version": dataset.get("bench_version"),
        "surface_salt": SURFACE_SALT,
        "posture": "shadow",
        "per_case": per_case,
        "summary": summarize(per_case, report),
    }


def _tool_notes(verdict: dict[str, Any], evidence_source: str) -> list[str]:
    notes: list[str] = []
    if not verdict["settled"]:
        notes.append(
            "no catalog evidence for this case (no completion log, no relay record); catalog gate not replayed"
        )
        return notes
    fired = [f for f in verdict["findings"] if f in GATE_FINDINGS]
    if fired:
        notes.append(
            f"v13 catalog gate (shadow, {evidence_source}): {', '.join(fired)}"
        )
    if verdict["safe_harbor"]:
        notes.append(
            f"safe harbor: {verdict['safe_harbor']} (published top-k: {', '.join(verdict['top_k'])})"
        )
    return notes


def _twin_post_pass(groups: dict[str, list[dict[str, Any]]]) -> None:
    """Twin/pair rules. Decision and as-of twins: identical decision class
    across the group is concordant (R1 zeroes the group; R2, the pair product,
    is reported beside it). Metamorphic groups: a counterfactual member that
    answered like its base is the counterfactual_insensitive pair."""
    for group, members in groups.items():
        if len(members) < 2:
            continue
        relations = {m["relation"] for m in members}
        if relations & DECISION_TWIN_RELATIONS:
            classes = {m["_decision_class"] for m in members}
            if len(classes) == 1:
                corrects = [m["_correct"] for m in members]
                pair_product: float | None = None
                if all(c is not None for c in corrects):
                    pair_product = 1.0 if all(bool(c) for c in corrects) else 0.0
                for m in members:
                    m["findings"].append(FINDING_TWIN_CONCORDANT)
                    m["would_zero"] = True
                    note = f"twin group {group}: identical decision across members (twin_concordant; R1 concordant-zero"
                    if pair_product is not None:
                        note += f", R2 pair-product {pair_product:.0f}"
                    m["notes"].append(note + ")")
            continue
        base = [m for m in members if m["relation"] == METAMORPHIC_BASE]
        counter = [m for m in members if m["relation"] == METAMORPHIC_COUNTERFACTUAL]
        if (
            len(base) == 1
            and len(counter) == 1
            and base[0]["_decision_class"] == counter[0]["_decision_class"]
        ):
            for m in (base[0], counter[0]):
                m["findings"].append(FINDING_COUNTERFACTUAL_INSENSITIVE)
                m["would_zero"] = True
                m["notes"].append(
                    f"twin group {group}: counterfactual member answered like its base "
                    "(counterfactual_insensitive; base + counterfactual pair 0)"
                )


def summarize(
    per_case: list[dict[str, Any]], report: dict[str, Any] | None
) -> dict[str, Any]:
    counts = dict.fromkeys(GATE_FINDINGS, 0)
    for record in per_case:
        for finding in record["findings"]:
            if finding in counts:
                counts[finding] += 1
    tool_cases = [r for r in per_case if r["kind"] == "tool"]
    memory_cases = [r for r in per_case if r["kind"] == "memory"]
    summary: dict[str, Any] = {
        "tool_cases": len(tool_cases),
        "memory_cases": len(memory_cases),
        "settled_tool_cases": sum(
            1 for r in tool_cases if FINDING_EVIDENCE_UNAVAILABLE not in r["findings"]
        ),
        "would_zero_cases": sum(1 for r in per_case if r["would_zero"]),
        "findings": counts,
    }
    scored = {
        str(s["case_id"]): s
        for s in _as_list((report or {}).get("per_case"))
        if isinstance(s, dict) and s.get("case_id")
    }
    if scored:
        gated = {r["case_id"] for r in per_case if r["would_zero"]}
        tool_scores = [
            (float(s.get("tool_score") or s.get("score") or 0.0), cid)
            for cid, s in scored.items()
            if s.get("kind") == "tool"
        ]
        memory_scores = [
            (float(s.get("score") or 0.0), cid)
            for cid, s in scored.items()
            if s.get("kind") == "memory"
        ]

        def mean(values: list[float]) -> float:
            return sum(values) / len(values) if values else 0.0

        pre_tool = mean([v for v, _ in tool_scores])
        pre_memory = mean([v for v, _ in memory_scores])
        post_tool = mean([0.0 if cid in gated else v for v, cid in tool_scores])
        post_memory = mean([0.0 if cid in gated else v for v, cid in memory_scores])

        def composite(tool: float, memory: float) -> float:
            if tool_scores and memory_scores:
                return 0.5 * tool + 0.5 * memory
            return tool if tool_scores else memory

        pre = composite(pre_tool, pre_memory)
        post = composite(post_tool, post_memory)
        summary["gate_induced_loss_shadow"] = {
            "composite_before_gates": round(pre, 6),
            "composite_if_enforced": round(post, 6),
            "loss": round(pre - post, 6),
            "note": "per-case means only; run-level factors are not re-applied",
        }
    return summary


def format_gates(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "",
        f"=== Bench v13 gates (shadow replay, surface salt {result['surface_salt']}) ===",
        f"tool cases:          {summary['tool_cases']} ({summary['settled_tool_cases']} with catalog evidence)",
        f"memory cases:        {summary['memory_cases']}",
        f"would-zero cases:    {summary['would_zero_cases']}",
    ]
    for finding, count in summary["findings"].items():
        lines.append(f"  {finding:<32} {count}")
    loss = summary.get("gate_induced_loss_shadow")
    if loss:
        lines.append(
            f"gate-induced loss (shadow): {loss['composite_before_gates']:.3f} -> "
            f"{loss['composite_if_enforced']:.3f} (loss {loss['loss']:.3f})"
        )
    flagged = [r for r in result["per_case"] if r["notes"]]
    if flagged:
        lines.append("")
        lines.append("per-case notes:")
        for record in flagged:
            lines.append(
                f"- {record['case_id']} [{record['kind']}/{record['category']}]"
            )
            lines.extend(f"    {note}" for note in record["notes"])
    lines.extend(
        [
            "",
            "Every v13 gate is shadow in v13.0: nothing above moved this score. A",
            "would-zero note is what the validator's relay would record on the",
            "same behavior; fix the served path, do not tune the note away.",
            "Decision/as-of twin notes need the scorer report (per_case relation);",
            "without --report only metamorphic base/counterfactual pairs are grouped.",
        ]
    )
    return "\n".join(lines)


def load_json(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} is not a JSON object")
    return decoded


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            decoded = json.loads(line)
            if isinstance(decoded, dict):
                entries.append(decoded)
    return entries


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay the public Bench v13 gates against kept rehearsal artifacts."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="pass-off dataset artifact (<run_id>.json)",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="run transcript (<run_id>.transcript.json)",
    )
    parser.add_argument(
        "--completions", type=Path, help="harness completion log (completions.jsonl)"
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="rehearsal report JSON (for scores and gate-induced loss)",
    )
    parser.add_argument(
        "--projection",
        type=Path,
        help="private V9 projection manifest (<run_id>.projection.json)",
    )
    parser.add_argument("--out", type=Path, help="write the gate result JSON here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report: dict[str, Any] | None = load_json(args.report) if args.report else None
    if report is not None and isinstance(report.get("report"), dict):
        report = _as_dict(report.get("report"))
    result = evaluate_run(
        load_json(args.dataset),
        load_json(args.transcript),
        load_jsonl(args.completions) if args.completions else [],
        report,
        load_json(args.projection) if args.projection else None,
    )
    print(format_gates(result))
    if args.out:
        args.out.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
