from __future__ import annotations

import json

import pytest

from dittobench_reranker.contract import (
    CONTRACT,
    CONTRACT_ID,
    SPECIAL_TOKEN_IDS,
    DethroneRule,
    SeasonContract,
    dethrone_threshold,
)


def test_contract_matches_season_decisions() -> None:
    # Pinned decisions (SN-202 / SN-203); changing one is a contract revision.
    assert CONTRACT.contract_id == CONTRACT_ID == "reranker-season-v1"
    assert CONTRACT.latency.budget_ms_p50 == 50.0
    assert CONTRACT.scoring.pool_size == 20
    assert CONTRACT.scoring.max_len == 256
    assert CONTRACT.scoring.rrf_k == 60.0
    assert CONTRACT.scoring.ce_weight == 0.7
    assert CONTRACT.scoring.ndcg_k == 10
    assert CONTRACT.onnxruntime_version == "1.22.0"


def test_special_token_ids_match_product_tokenizer() -> None:
    assert SPECIAL_TOKEN_IDS == {"[PAD]": 0, "[UNK]": 100, "[CLS]": 101, "[SEP]": 102}


def test_digest_is_stable_and_sensitive() -> None:
    assert CONTRACT.digest() == SeasonContract().digest()
    assert len(CONTRACT.digest()) == 64
    altered = SeasonContract(saturation_quiet_days=CONTRACT.saturation_quiet_days + 1)
    assert altered.digest() != CONTRACT.digest()


def test_canonical_json_is_sorted_and_compact() -> None:
    raw = CONTRACT.canonical_json()
    decoded = json.loads(raw)
    assert raw == json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    assert decoded["allowed_base_model_licenses"] == sorted(
        decoded["allowed_base_model_licenses"]
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [(0.0, 0.005), (0.001, 0.005), (0.01, 0.0164)],
)
def test_dethrone_threshold_uses_larger_of_margin_and_band(
    stderr: float, expected: float
) -> None:
    assert dethrone_threshold(stderr) == pytest.approx(expected)


def test_dethrone_threshold_rejects_negative_stderr() -> None:
    with pytest.raises(ValueError):
        dethrone_threshold(-0.1)


def test_dethrone_threshold_accepts_custom_rule() -> None:
    assert dethrone_threshold(0.0, DethroneRule(min_margin=0.02, z=2.0)) == 0.02
