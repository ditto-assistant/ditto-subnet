"""Canonical taxonomy strings for DittoBench.

The string values are the on-disk contract fixture JSONL files use; do NOT
rename them without bumping ``ditto.bench.SCHEMA_VERSION``. Validators and
miner harnesses must agree on these strings byte-for-byte.
"""

from __future__ import annotations

from enum import StrEnum


class Mechanism(StrEnum):
    """On-chain incentive mechanisms exposed by the DittoBench subnet."""

    CORE = "ditto_core"
    """Mechanism 0: tool-calling recall and accuracy (``DittoCore``)."""

    RETRIEVAL = "ditto_retrieval"
    """Mechanism 1: memory retrieval + grounded answer quality."""


class Suite(StrEnum):
    """Suite identifiers used by fixture JSONL files and the CLI runner."""

    TOOL_CALL = "tool_use"
    RETRIEVAL = "retrieval"
    RESPONSE_QUALITY = "response_quality"
    LONGMEMEVAL = "longmemeval"


class RetrievalCategory(StrEnum):
    """DittoRetrieval category taxonomy.

    The taxonomy is intentionally narrow so miners cannot game the benchmark
    by optimizing for a noisy long tail of unrelated categories.
    """

    SINGLE_NEEDLE_RECENT = "single_needle_recent"
    SINGLE_NEEDLE_OLD = "single_needle_old"
    SEMANTIC_PARAPHRASE = "semantic_paraphrase"
    MULTI_NEEDLE = "multi_needle"
    SUBJECT_SCOPED = "subject_scoped"
    STALE_OUTSIDE_WINDOW = "stale_outside_window"
    CONTRADICTION_UPDATE = "contradiction_update"
    STM_ONLY = "stm_only"
    STM_WITH_DISTRACTORS = "stm_with_distractors"
    SHORT_TERM_MEMORY = "short_term_memory"  # legacy alias accepted on load
    MCP_PARITY = "mcp_parity"
    LONGMEMEVAL_EVIDENCE = "longmemeval_evidence"


class CoreDomain(StrEnum):
    """DittoCore user-facing capability buckets.

    Each fixture's ``domain`` field MUST be one of these values; the CLI
    rejects unknown domains so a typo never silently lands in the public
    repo.
    """

    PERSONAL_RECALL = "personal_recall_routing"
    CURRENT_EVENTS = "current_events_routing"
    LINK_INGESTION = "link_ingestion"
    IMAGE_GENERATION = "image_generation"
    GROUNDED_CITATION = "grounded_citation_request"
    SAFETY_PRIVACY = "safety_privacy_refusal"
    AMBIGUOUS_CLARIFICATION = "ambiguous_query_clarification"
    STM_VS_LTM_DISPATCH = "stm_vs_ltm_dispatch"
    MULTI_STEP_PLANNING = "multi_step_planning"
    TOOL_USE_ABSTENTION = "tool_use_abstention"


KNOWN_RETRIEVAL_CATEGORIES: frozenset[str] = frozenset(
    c.value for c in RetrievalCategory
)
"""Set form of :class:`RetrievalCategory` for cheap membership tests."""

KNOWN_CORE_DOMAINS: frozenset[str] = frozenset(d.value for d in CoreDomain)
"""Set form of :class:`CoreDomain` for cheap membership tests."""


def is_known_retrieval_category(category: str) -> bool:
    """Return True if ``category`` is in the canonical DittoRetrieval taxonomy."""
    return category in KNOWN_RETRIEVAL_CATEGORIES


def is_known_core_domain(domain: str) -> bool:
    """Return True if ``domain`` is in the canonical DittoCore taxonomy."""
    return domain in KNOWN_CORE_DOMAINS
