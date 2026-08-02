from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.anti_copy_comparison import compare_anti_copy_pair
from ditto.api_server.fingerprint import reference_corpus_provenance
from ditto.db.queries.scores import LedgerRow

_NOW = datetime(2026, 7, 16, tzinfo=UTC)
_CORPUS = reference_corpus_provenance()["corpus_id"]


def _fp(
    values: set[str], *, version: int | str = 2, corpus: str | None = _CORPUS
) -> dict:
    fingerprint = {
        "v": version,
        "k": 256,
        "card": len(values),
        "m": sorted(values),
    }
    if corpus is not None:
        fingerprint["corpus"] = corpus
    return fingerprint


def _row(
    *,
    agent_id: int,
    miner: str,
    first_seen: datetime,
    sha256: str,
    content: dict | None,
    structural: dict | None = None,
    prompt: dict | None = None,
    normalized: str | None = None,
    size: int | None = 500_000,
    coldkey: str | None = None,
) -> LedgerRow:
    return LedgerRow(
        miner_hotkey=miner,
        agent_id=UUID(int=agent_id),
        composite=0.8,
        tool_mean=0.8,
        memory_mean=0.8,
        first_seen=first_seen,
        sha256=sha256,
        size_bytes=size,
        run_id="run",
        seed=1,
        validator_hotkey="validator",
        signature=None,
        status=AgentStatus.SCORED,
        miner_coldkey=coldkey,
        content_fingerprint=content,
        structural_fingerprint=structural,
        normalized_source_hash=normalized,
        prompt_fingerprint=prompt,
    )


def test_adapter_returns_wire_safe_aggregate_copy_evidence() -> None:
    residual = {f"{i:016x}" for i in range(12)}
    structural = _fp(residual, version=1, corpus=None)
    prompt = _fp(residual, version="p2")
    reference = _row(
        agent_id=1,
        miner="reference-miner",
        first_seen=_NOW,
        sha256="a" * 64,
        content=_fp(residual),
        structural=structural,
        prompt=prompt,
    )
    candidate = _row(
        agent_id=2,
        miner="candidate-miner",
        first_seen=_NOW + timedelta(seconds=1),
        sha256="b" * 64,
        content=_fp(residual),
        structural=structural,
        prompt=prompt,
    )

    result = compare_anti_copy_pair(candidate=candidate, reference=reference)

    assert result.current_decision == "hold"
    assert result.availability == "available"
    assert result.bulk_eligible is False
    assert result.triggered_signal == "lexical"
    assert result.chronology_direction == "reference_earlier"
    assert result.lexical.containment == 1.0
    assert result.lexical.candidate_cardinality == 12
    assert result.structural.decision_role == "advisory"
    assert result.prompt.decision_role == "advisory"
    wire = result.to_wire()
    serialized = json.dumps(wire)
    for forbidden in ("sha256", "normalized_source_hash", "artifact", "path", '"m"'):
        assert forbidden not in serialized


def test_cross_corpus_pair_is_clear_without_structural_or_size_fallback() -> None:
    values = {f"{i:016x}" for i in range(12)}
    structural = _fp(values, version=1, corpus=None)
    reference = _row(
        agent_id=1,
        miner="reference-miner",
        first_seen=_NOW,
        sha256="a" * 64,
        content=_fp(values, corpus="older-corpus"),
        structural=structural,
    )
    candidate = _row(
        agent_id=2,
        miner="candidate-miner",
        first_seen=_NOW + timedelta(seconds=1),
        sha256="b" * 64,
        content=_fp(values),
        structural=structural,
        size=500_001,
    )

    result = compare_anti_copy_pair(candidate=candidate, reference=reference)

    assert result.current_decision == "clear"
    assert result.triggered is False
    assert result.triggered_signal is None
    assert result.lexical.compatible is False
    assert result.lexical.jaccard is None
    assert result.bulk_eligible is False


def test_independent_residuals_are_clear_despite_structural_overlap() -> None:
    structural_values = {f"{i:016x}" for i in range(30)}
    reference = _row(
        agent_id=1,
        miner="reference-miner",
        first_seen=_NOW,
        sha256="a" * 64,
        content=_fp({f"a{i:015x}" for i in range(12)}),
        structural=_fp(structural_values, version=1, corpus=None),
    )
    candidate = _row(
        agent_id=2,
        miner="candidate-miner",
        first_seen=_NOW + timedelta(seconds=1),
        sha256="b" * 64,
        content=_fp({f"b{i:015x}" for i in range(12)}),
        structural=_fp(structural_values, version=1, corpus=None),
        size=500_001,
    )

    result = compare_anti_copy_pair(candidate=candidate, reference=reference)

    assert result.current_decision == "clear"
    assert result.lexical.above_threshold is False
    assert result.structural.above_threshold is True
    assert result.structural.decision_role == "advisory"
    assert result.bulk_eligible is True


def test_later_reference_is_not_chronology_eligible() -> None:
    values = {f"{i:016x}" for i in range(12)}
    candidate = _row(
        agent_id=1,
        miner="candidate-miner",
        first_seen=_NOW,
        sha256="a" * 64,
        content=_fp(values),
    )
    reference = _row(
        agent_id=2,
        miner="reference-miner",
        first_seen=_NOW + timedelta(seconds=1),
        sha256="a" * 64,
        content=_fp(values),
    )

    result = compare_anti_copy_pair(candidate=candidate, reference=reference)

    assert result.chronology_direction == "candidate_earlier"
    assert result.chronology_eligible is False
    assert result.current_decision == "clear"
    assert result.exact_byte_match is True
    assert result.bulk_eligible is False


def test_same_miner_pair_is_explicitly_excluded() -> None:
    values = {f"{i:016x}" for i in range(12)}
    reference = _row(
        agent_id=1,
        miner="same-miner",
        first_seen=_NOW,
        sha256="a" * 64,
        content=_fp(values),
    )
    candidate = _row(
        agent_id=2,
        miner="same-miner",
        first_seen=_NOW + timedelta(seconds=1),
        sha256="a" * 64,
        content=_fp(values),
    )

    result = compare_anti_copy_pair(candidate=candidate, reference=reference)

    assert result.same_miner_excluded is True
    assert result.chronology_eligible is False
    assert result.current_decision == "excluded"
    assert result.exact_byte_match is False


def test_same_coldkey_pair_across_hotkeys_is_explicitly_excluded() -> None:
    values = {f"{i:016x}" for i in range(12)}
    reference = _row(
        agent_id=1,
        miner="old-hotkey",
        coldkey="shared-coldkey",
        first_seen=_NOW,
        sha256="a" * 64,
        content=_fp(values),
    )
    candidate = _row(
        agent_id=2,
        miner="new-hotkey",
        coldkey="shared-coldkey",
        first_seen=_NOW + timedelta(seconds=1),
        sha256="a" * 64,
        content=_fp(values),
    )

    result = compare_anti_copy_pair(candidate=candidate, reference=reference)

    assert result.same_miner_excluded is True
    assert result.current_decision == "excluded"
    assert result.exact_byte_match is False
