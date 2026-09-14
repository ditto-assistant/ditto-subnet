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
* ``served_text_not_model_emitted`` — the claim-span provenance normaliser:
  the graded claim's canonical value tokens must appear in some model
  completion (message text, tool-call arguments, structured output).
* ``slot_not_in_prose`` — a populated ``answer`` slot whose value has no
  equivalent in ``final_text`` (numbers compare canonically and across the
  minor/major unit alternative, so ``411067`` beside ``$4,110.67`` passes).
* ``answer_in_prompt`` — the causal gate with the records exemption: the graded
  value tokens all appear in a harness-authored span and none of them is
  covered by a ``/seed`` record, a retrieved-memory context message, a delivered
  tool result, or the case's own question.
* ``twin_concordant`` / ``counterfactual_insensitive`` — the twin/pair
  post-pass: identical decision class across a decision/as-of twin group, or a
  counterfactual member answered like its base.

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

_STOPWORDS = frozenset(
    """the and for with that this from you your can please one more use are was
    were have has had not but any all into its our out about what which when
    where who how why just like then than too very will would should could
    there here some them they their thing tool tools given return returns
    default""".split()
)


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


# ─── Provenance normaliser (public) ──────────────────────────────────────────

NUMBER_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")
ALNUM_RE = re.compile(r"[a-z0-9]+")
_ANSWER_LABEL_RE = re.compile(r"(?im)^[\s*_`#>\-]*answer\s*:\s*")
_MIN_STRING_TOKEN_LEN = 4
_FOLD_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "ʼ": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "\u00a0": " ",
        "*": " ",
        "_": " ",
        "`": " ",
        "#": " ",
    }
)


def canonical_number(raw: str) -> str:
    """Port of the scorer's ``canonicalNumber``: strip ``$`` and grouping
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
    if not (int_part + frac_part).isdigit():
        return ""
    out = int_part + ("." + frac_part if frac_part else "")
    if negative and out != "0":
        out = "-" + out
    return out


def fold_text(text: str) -> str:
    """Request-independent normalisation: NFKC, casefold, quote/dash folding,
    markdown emphasis stripped, ``Answer:`` labels stripped, whitespace
    collapsed."""
    folded = unicodedata.normalize("NFKC", text).translate(_FOLD_MAP).casefold()
    folded = _ANSWER_LABEL_RE.sub("", folded)
    return " ".join(folded.split())


def value_tokens(text: str) -> set[str]:
    """The canonical value-token set of a span: every canonical number plus
    every alphanumeric token of at least four characters that is not purely
    numeric (the scorer's ``valueTokenSet`` vocabulary)."""
    folded = fold_text(text)
    tokens: set[str] = set()
    for match in NUMBER_RE.findall(folded):
        canonical = canonical_number(match)
        if canonical:
            tokens.add(canonical)
    for token in ALNUM_RE.findall(folded):
        if len(token) >= _MIN_STRING_TOKEN_LEN and not token.isdigit():
            tokens.add(token)
    return tokens


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
    """Whole-token containment of a folded phrase in folded text."""
    text_f, phrase_f = fold_text(text), fold_text(phrase)
    if not phrase_f:
        return False
    return (
        re.search(rf"(?<![a-z0-9]){re.escape(phrase_f)}(?![a-z0-9])", text_f)
        is not None
    )


def slot_in_prose(slot: str, final_text: str) -> bool:
    """Whether the ``answer`` slot has an equivalent value asserted in the
    prose: bounded containment of the folded slot, or every slot number having
    a canonical or unit-alternative match among the prose numbers."""
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


def provenance_missing(
    claim_tokens: set[str], completion_texts: Iterable[str]
) -> set[str]:
    """The graded-claim tokens no model completion emitted (empty = pass)."""
    emitted: set[str] = set()
    for text in completion_texts:
        emitted |= value_tokens(text)
    return {token for token in claim_tokens if token not in emitted}


def answer_in_prompt(
    claim_tokens: set[str], harness_texts: Iterable[str], exempt_texts: Iterable[str]
) -> bool:
    """Causal gate: every graded-claim token appears in a harness-authored span
    after subtracting tokens covered by records, tool results, or the question."""
    if not claim_tokens:
        return False
    authored: set[str] = set()
    for text in harness_texts:
        authored |= value_tokens(text)
    exempt: set[str] = set()
    for text in exempt_texts:
        exempt |= value_tokens(text)
    residual = authored - exempt
    return claim_tokens <= residual


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
) -> dict[str, Any]:
    """Pure port of the v13 catalog gate for one tool case."""
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
    offered = set(tools_offered)
    catalog_present = bool(offered)
    no_completion = completions_total == 0
    catalog_absent = not no_completion and not catalog_present
    top_k = semantic_top_k(prompt, catalog, SAFE_HARBOR_TOP_K)
    verdict["top_k"] = top_k
    retained_top_k = bool(top_k) and all(name in offered for name in top_k)
    if no_completion:
        verdict["findings"].append(FINDING_NO_MODEL_COMPLETION)
    if catalog_absent:
        verdict["findings"].append(FINDING_CATALOG_ABSENT)

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
        elif catalog_present and not no_completion and not tempting:
            verdict["safe_harbor"] = SAFE_HARBOR_NONEMPTY_DECLARATIVE
        else:
            verdict["findings"].append(FINDING_RESTRAINT_WITHOUT_OFFER)
            verdict["zero"] = True
        if verdict["safe_harbor"]:
            verdict["findings"].append(FINDING_SAFE_HARBOR)
        return verdict

    missing = sorted(
        name
        for name in expected_tools
        if name not in MEMORY_TOOLS and name not in offered
    )
    if not missing:
        return verdict
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


def _expected_forms(case: dict[str, Any]) -> list[str]:
    forms: list[str] = []
    if case.get("expected_answer"):
        forms.append(str(case["expected_answer"]))
    forms.extend(str(v) for v in _as_list(case.get("accept_any")))
    forms.extend(str(v) for v in _as_list(case.get("answer_items")))
    for alternatives in _as_list(case.get("answer_item_accept_any")):
        forms.extend(str(v) for v in _as_list(alternatives))
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
    for case in _as_list(dataset.get("memory_cases")):
        if isinstance(case, dict):
            for memory in _as_list(case.get("seed_memories")):
                if isinstance(memory, dict):
                    texts.append(
                        f"{memory.get('prompt', '')}\n{memory.get('response', '')}"
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
    provenance = case.get("v10_provenance")
    if isinstance(provenance, dict) and provenance.get("relation"):
        return str(provenance["relation"])
    if case.get("twin_relation"):
        return str(case["twin_relation"])
    if scored and scored.get("relation"):
        return str(scored["relation"])
    return ""


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
        ]
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
        expected_forms = _expected_forms(case)
        expected_tokens: set[str] = set()
        for form in expected_forms:
            expected_tokens |= value_tokens(form)
        served_tokens = value_tokens(final_text) | (
            value_tokens(slot) if slot else set()
        )
        claim_tokens = value_tokens(slot) if slot else (expected_tokens & served_tokens)
        findings: list[str] = []
        notes: list[str] = []
        would_zero = False
        if slot and not slot_in_prose(slot, final_text):
            findings.append(FINDING_SLOT_NOT_IN_PROSE)
            notes.append(
                "answer slot has no equivalent value asserted in final_text (slot_not_in_prose)"
            )
            would_zero = True
        if claim_tokens:
            if entry is None:
                findings.append(FINDING_PROVENANCE_UNAVAILABLE)
                notes.append(
                    "no completion log for this case; provenance and causal gates not replayed"
                )
            else:
                missing = provenance_missing(claim_tokens, _completion_texts(entry))
                if missing:
                    findings.append(FINDING_SERVED_TEXT_NOT_MODEL_EMITTED)
                    notes.append(
                        "graded claim tokens absent from every model completion "
                        f"({len(missing)} of {len(claim_tokens)}): served_text_not_model_emitted"
                    )
                    would_zero = True
                exempt = list(_as_list(entry.get("record_spans"))) + list(
                    _as_list(entry.get("tool_results"))
                )
                exempt.append(str(entry.get("user_input") or ""))
                exempt.extend(seed_texts)
                if answer_in_prompt(
                    claim_tokens, _as_list(entry.get("harness_spans")), exempt
                ):
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
            "_decision_class": _decision_class(
                response,
                expected_forms
                + [str(v) for v in _as_list(case.get("distractor_answers"))],
            ),
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
