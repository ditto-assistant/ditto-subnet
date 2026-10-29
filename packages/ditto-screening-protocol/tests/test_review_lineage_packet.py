"""Shape rules a sealed lineage packet enforces at the protocol boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from ditto_screening_protocol import seal_review_lineage_packet

_AGENT = uuid4()
_PARENT = uuid4()


def _parent() -> dict[str, Any]:
    return {
        "agent_id": _PARENT,
        "version": 1,
        "artifact_sha256": "ab" * 32,
        "status": "rejected",
        "screening_policy_version": 13,
        "submitted_at": datetime.now(UTC),
    }


def _ruling() -> dict[str, Any]:
    return {
        "agent_id": _PARENT,
        "version": 1,
        "court": "quarantine",
        "resolution": "reject",
        "reason": "answer() overrides the model",
        "policy_version": 13,
        "decided_at": datetime.now(UTC),
    }


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "agent_id": _AGENT,
        "attempt_id": uuid4(),
        "artifact_sha256": "de" * 32,
        "policy_version": 13,
        "parent": _parent(),
        "history": [_parent()],
        "scope": "diff",
        "full_review_reasons": [],
        "rulings": [_ruling()],
        "findings": [],
    }
    body.update(overrides)
    return body


def test_a_diff_packet_seals() -> None:
    packet = seal_review_lineage_packet(_body())
    assert packet.scope == "diff"
    assert packet.history[0].agent_id == _PARENT


def test_diff_scope_needs_rulings_or_findings() -> None:
    with pytest.raises(ValueError, match="rulings or findings"):
        seal_review_lineage_packet(_body(rulings=[]))


def test_full_scope_with_no_history_is_the_published_shape() -> None:
    packet = seal_review_lineage_packet(
        _body(scope="full", full_review_reasons=["no-review-history"], rulings=[])
    )
    assert packet.full_review_reasons == ["no-review-history"]


def test_history_must_start_at_the_parent() -> None:
    with pytest.raises(ValueError, match="start at the parent"):
        seal_review_lineage_packet(_body(history=[]))
    other = _parent() | {"agent_id": uuid4()}
    with pytest.raises(ValueError, match="start at the parent"):
        seal_review_lineage_packet(_body(history=[other, _parent()]))


def test_full_scope_names_its_reasons() -> None:
    with pytest.raises(ValueError, match="name its reasons"):
        seal_review_lineage_packet(_body(scope="full"))
    with pytest.raises(ValueError, match="name its reasons"):
        seal_review_lineage_packet(_body(full_review_reasons=["diff-too-large"]))


def test_a_classic_packet_digest_ignores_the_absent_reference_kind() -> None:
    """Packets without a reference kind keep the digest they had before it."""
    import hashlib
    import json

    from ditto_screening_protocol.review_lineage import ReviewLineagePacketBody

    body = _body()
    packet = seal_review_lineage_packet(body)
    assert packet.reference_kind is None
    legacy = ReviewLineagePacketBody.model_validate(body).model_dump(
        mode="json", exclude={"reference_kind"}
    )
    expected = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert packet.packet_sha256 == expected


def test_a_reference_kind_is_sealed_into_the_digest() -> None:
    body = _body()
    plain = seal_review_lineage_packet(body)
    released = seal_review_lineage_packet({**body, "reference_kind": "released_base"})
    assert released.reference_kind == "released_base"
    assert released.packet_sha256 != plain.packet_sha256
    with pytest.raises(ValueError):
        seal_review_lineage_packet({**body, "reference_kind": "someone_else"})
