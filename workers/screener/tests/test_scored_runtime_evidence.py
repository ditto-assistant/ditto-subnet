"""Release-bound scorer packet controls for the Artemis L2 lead.

Artemis is a CLEAR candidate, not a labeled safe verdict. Sky v7 is a
confirmed V13 REJECT control for semantic retry/selection (I3).
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from ditto_screener.scored_runtime_evidence import (
    fetch_runtime_evidence,
    validate_runtime_evidence,
)

ARTEMIS_V12 = {
    "agent_id": "51138c6f-9ca3-4fc1-bb23-fea7486aa690",
    "artifact_sha256": (
        "5a20e68334139dc322c1b21ea7601248a4eb14db5c7018a1a65b9b3c7942c67c"
    ),
    "attempt_id": "0b772e0b-eed0-433d-b86b-cda722093ec9",
    "policy_version": 13,
    "label": "clear_candidate_only",
}
SKY_V7_REJECT = {
    "agent_id": "7525c382-cbbf-4352-bf1a-6ab57f5195dd",
    "artifact_sha256": (
        "1bebee75a3f40dfffcd6a946e469a19412747bc92256508907cc4db03a33d1a9"
    ),
    "attempt_id": "a2f8c12c-2cdf-427d-965e-ed784c59882f",
    "policy_version": 13,
    "finding_digest": (
        "0e9a0e08dde531827a438688901b54f51c4d2b3053521dd50ac1eac6fc2e020d"
    ),
    "label": "confirmed_reject_i3_semantic_retry_or_selection",
}
REVISION = "a" * 40
KEYS = ["DITTOBENCH_DB", "DITTOBENCH_MODEL", "DITTOBENCH_PROVIDER"]


def _capabilities() -> dict[str, object]:
    material = "scored-runtime-env-v1\n13\n" + REVISION + "\n" + "\n".join(KEYS)
    return {
        "source_revision": REVISION,
        "source_revision_origin": "binary",
        "source_revision_mismatch": False,
        "scored_runtime_env": {
            "bench_version": 13,
            "scope": "scorer-injected-env-only",
            "source_revision": REVISION,
            "injected_keys": KEYS,
            "sha256": hashlib.sha256(material.encode()).hexdigest(),
        },
    }


def test_artemis_packet_supports_only_scoped_environment_reasoning() -> None:
    assert ARTEMIS_V12["label"] == "clear_candidate_only"
    evidence = validate_runtime_evidence(_capabilities(), expected_revision=REVISION)
    assert "DITTOBENCH_COMPLETION_LOG" not in evidence["injected_keys"]
    assert evidence["scope"] == "scorer-injected-env-only"
    assert "image ENV" in evidence["limits"]
    # The packet alone does not tell us whether the artifact enables the sink
    # through image ENV or a source default; it has no verdict field.
    assert "verdict" not in evidence


def test_known_reject_is_not_cleared_by_absent_injected_key() -> None:
    evidence = validate_runtime_evidence(_capabilities(), expected_revision=REVISION)
    assert SKY_V7_REJECT["label"] == "confirmed_reject_i3_semantic_retry_or_selection"
    assert "DITTOBENCH_COMPLETION_LOG" not in evidence["injected_keys"]
    assert "verdict" not in evidence


@pytest.mark.parametrize(
    "mutation",
    [
        {"source_revision_origin": "env"},
        {"source_revision_mismatch": True},
        {"source_revision": "b" * 40},
        {"scored_runtime_env": None},
    ],
)
def test_stale_or_untrusted_packet_refused(mutation: dict[str, object]) -> None:
    packet = _capabilities()
    packet.update(mutation)
    with pytest.raises(ValueError):
        validate_runtime_evidence(packet, expected_revision=REVISION)


def test_wrong_digest_and_unsorted_keys_refused() -> None:
    packet = _capabilities()
    raw = packet["scored_runtime_env"]
    assert isinstance(raw, dict)
    raw["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_runtime_evidence(packet, expected_revision=REVISION)
    packet = _capabilities()
    raw = packet["scored_runtime_env"]
    assert isinstance(raw, dict)
    raw["injected_keys"] = list(reversed(KEYS))
    with pytest.raises(ValueError):
        validate_runtime_evidence(packet, expected_revision=REVISION)


@pytest.mark.asyncio
async def test_fetch_requires_exact_https_endpoint_without_redirect() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://scorer.example/v1/capabilities"
        return httpx.Response(200, json=_capabilities())

    evidence = await fetch_runtime_evidence(
        "https://scorer.example/v1/capabilities",
        expected_revision=REVISION,
        transport=httpx.MockTransport(responder),
    )
    assert evidence["source_revision"] == REVISION
    with pytest.raises(ValueError):
        await fetch_runtime_evidence(
            "http://scorer.example/v1/capabilities",
            expected_revision=REVISION,
            transport=httpx.MockTransport(responder),
        )
