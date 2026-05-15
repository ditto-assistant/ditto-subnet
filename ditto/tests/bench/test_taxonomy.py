"""Unit tests for ditto.bench.loader.taxonomy."""

from __future__ import annotations

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


def test_mechanism_string_values_are_on_chain_identifiers() -> None:
    """Mechanism enum values must equal the on-chain mechanism identifiers."""
    assert str(Mechanism.CORE) == "ditto_core"
    assert str(Mechanism.RETRIEVAL) == "ditto_retrieval"


def test_suite_string_values_match_fixture_dir_names() -> None:
    """Suite enum values are used as on-disk identifiers (loader, CLI)."""
    assert str(Suite.TOOL_CALL) == "tool_use"
    assert str(Suite.RETRIEVAL) == "retrieval"
    assert str(Suite.RESPONSE_QUALITY) == "response_quality"
    assert str(Suite.LONGMEMEVAL) == "longmemeval"


def test_known_retrieval_categories_set_matches_enum() -> None:
    """The frozenset and enum must enumerate the same categories."""
    assert {c.value for c in RetrievalCategory} == KNOWN_RETRIEVAL_CATEGORIES


def test_known_core_domains_set_matches_enum() -> None:
    """The frozenset and enum must enumerate the same domains."""
    assert {d.value for d in CoreDomain} == KNOWN_CORE_DOMAINS


def test_is_known_retrieval_category_recognises_canonical_values() -> None:
    """Every canonical category is recognised; ad-hoc strings are rejected."""
    for category in RetrievalCategory:
        assert is_known_retrieval_category(category.value)
    assert not is_known_retrieval_category("not_a_category")


def test_is_known_core_domain_recognises_canonical_values() -> None:
    """Every canonical domain is recognised; ad-hoc strings are rejected."""
    for domain in CoreDomain:
        assert is_known_core_domain(domain.value)
    assert not is_known_core_domain("not_a_domain")


def test_legacy_short_term_memory_category_is_recognised() -> None:
    """short_term_memory is kept as a legacy alias for backward compatibility."""
    assert is_known_retrieval_category("short_term_memory")
