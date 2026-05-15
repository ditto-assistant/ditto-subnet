"""Fixture loaders + taxonomy enums for DittoBench.

Re-exports the case dataclasses and ``Load*Cases`` helpers so callers can
write ``from ditto.bench.loader import load_toolcall_cases, ToolCallCase``.
"""

from __future__ import annotations

from ditto.bench.loader.cases import (
    ArgMatcher,
    ArgMatcherKind,
    ExpectedToolCall,
    RetrievalCase,
    STMMessage,
    ToolCallCase,
    load_retrieval_cases,
    load_toolcall_cases,
)
from ditto.bench.loader.taxonomy import (
    KNOWN_CORE_DOMAINS,
    KNOWN_RETRIEVAL_CATEGORIES,
    CoreDomain,
    Mechanism,
    RetrievalCategory,
    Suite,
    is_known_core_domain,
    is_known_retrieval_category,
)

__all__ = [
    "ArgMatcher",
    "ArgMatcherKind",
    "ExpectedToolCall",
    "RetrievalCase",
    "STMMessage",
    "ToolCallCase",
    "load_retrieval_cases",
    "load_toolcall_cases",
    "KNOWN_CORE_DOMAINS",
    "KNOWN_RETRIEVAL_CATEGORIES",
    "CoreDomain",
    "Mechanism",
    "RetrievalCategory",
    "Suite",
    "is_known_core_domain",
    "is_known_retrieval_category",
]
