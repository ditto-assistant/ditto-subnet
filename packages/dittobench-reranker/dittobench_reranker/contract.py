"""Season 2 reranker contract: the single source of truth for every constant.

``docs/reranker-season.md`` explains these values. The scorer, the pool
generator's consumers, the screener and the miner CLI import them from here, so
a change is one diff and one contract digest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

CONTRACT_ID = "reranker-season-v1"

# Special-token ids the product tokenizer hard-codes
# (backend pkg/services/retrieval/crossencoder/tokenizer.go). A vocab that puts
# any other token at these ids would be scored on different inputs than the
# product would feed it.
SPECIAL_TOKEN_IDS: dict[str, int] = {
    "[PAD]": 0,
    "[UNK]": 100,
    "[CLS]": 101,
    "[SEP]": 102,
}

MODEL_INPUT_NAMES: tuple[str, ...] = ("input_ids", "attention_mask", "token_type_ids")

# The product binds this output by name (crossencoder.go NewDynamicAdvancedSession).
MODEL_OUTPUT_NAME = "logits"

# Operator domains the product's ONNX Runtime build executes. "" is the
# default ONNX domain; com.microsoft carries the contrib ops dynamic INT8
# quantization can emit. Anything else is a custom op the product cannot run.
ALLOWED_OPSET_DOMAINS: frozenset[str] = frozenset({"", "ai.onnx", "com.microsoft"})

# SPDX ids of base-model licences whose derivatives Ditto can ship.
ALLOWED_BASE_MODEL_LICENSES: frozenset[str] = frozenset(
    {"apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause"}
)

MIB = 1024 * 1024


@dataclass(frozen=True)
class ArtifactLimits:
    max_model_bytes: int = 100 * MIB
    max_vocab_bytes: int = 2 * MIB
    max_manifest_bytes: int = 64 * 1024
    # BERT-family vocabularies are ~30k; the ceiling only bounds parsing cost.
    max_vocab_tokens: int = 250_000


@dataclass(frozen=True)
class ScoringParams:
    pool_size: int = 20
    # The product reads ONNX_RERANK_MAXLEN (default 256); its README and
    # latency table use 128, and the incumbent scores higher at 128 on the
    # season pools (NDCG@10 0.792 vs 0.778). Shipping a champion sets 128.
    max_len: int = 128
    rrf_k: float = 60.0
    ce_weight: float = 0.7
    ndcg_k: int = 10
    recall_k: int = 10
    # One intra-op thread makes the float reduction order, and therefore the
    # logits, identical across runs on the same build.
    scoring_threads: int = 1


@dataclass(frozen=True)
class LatencyGate:
    # Peyton's budget is 50 ms where the incumbent takes ~14 ms (backend
    # crossencoder README). Absolute milliseconds depend on the CPU, so the
    # enforced gate is the same budget as a ratio to the incumbent measured
    # side by side on the same machine: 50 / 14 ~= 3.5.
    budget_ms_p50: float = 50.0
    incumbent_ms_p50: float = 14.0
    max_ratio_to_incumbent: float = 3.5
    threads: int = 4
    warmup_pools: int = 5
    measured_pools: int = 50


@dataclass(frozen=True)
class DethroneRule:
    min_margin: float = 0.005
    z: float = 1.64


@dataclass(frozen=True)
class Incumbent:
    base_model: str = "cross-encoder/ms-marco-TinyBERT-L2-v2"
    quantization: str = "int8-dynamic"
    license: str = "apache-2.0"
    # The exact files the product embeds (backend crossencoder/model/).
    model_sha256: str = (
        "d9c3f25f235fca2140fb1c15e63786e82c7ec9935e4426db4674be9a14d585fb"
    )
    vocab_sha256: str = (
        "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"
    )


@dataclass(frozen=True)
class SeasonContract:
    contract_id: str = CONTRACT_ID
    onnxruntime_version: str = "1.22.0"
    artifact: ArtifactLimits = field(default_factory=ArtifactLimits)
    scoring: ScoringParams = field(default_factory=ScoringParams)
    latency: LatencyGate = field(default_factory=LatencyGate)
    dethrone: DethroneRule = field(default_factory=DethroneRule)
    incumbent: Incumbent = field(default_factory=Incumbent)
    saturation_quiet_days: int = 21

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["special_token_ids"] = dict(SPECIAL_TOKEN_IDS)
        data["model_input_names"] = list(MODEL_INPUT_NAMES)
        data["model_output_name"] = MODEL_OUTPUT_NAME
        data["allowed_opset_domains"] = sorted(ALLOWED_OPSET_DOMAINS)
        data["allowed_base_model_licenses"] = sorted(ALLOWED_BASE_MODEL_LICENSES)
        return data

    def canonical_json(self) -> bytes:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode()

    def digest(self) -> str:
        """SHA-256 of the canonical contract, bound into every score report."""
        return hashlib.sha256(self.canonical_json()).hexdigest()


CONTRACT = SeasonContract()


def dethrone_threshold(
    paired_diff_stderr: float, rule: DethroneRule | None = None
) -> float:
    """Minimum mean-NDCG lead a challenger needs over the champion."""
    if paired_diff_stderr < 0:
        raise ValueError("standard error cannot be negative")
    rule = rule or CONTRACT.dethrone
    return max(rule.min_margin, rule.z * paired_diff_stderr)
