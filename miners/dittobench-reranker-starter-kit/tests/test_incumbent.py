"""The committed incumbent must be byte-identical to the product's model."""

from __future__ import annotations

import hashlib
from pathlib import Path

from dittobench_reranker.artifact import load_artifact
from dittobench_reranker.contract import CONTRACT

INCUMBENT = Path(__file__).parents[1] / "incumbent"


def test_incumbent_hashes_match_contract() -> None:
    model = hashlib.sha256((INCUMBENT / "model.onnx").read_bytes()).hexdigest()
    vocab = hashlib.sha256((INCUMBENT / "vocab.txt").read_bytes()).hexdigest()
    assert model == CONTRACT.incumbent.model_sha256
    assert vocab == CONTRACT.incumbent.vocab_sha256


def test_incumbent_passes_validation() -> None:
    art = load_artifact(INCUMBENT)
    assert art.manifest["base_model"] == CONTRACT.incumbent.base_model
    assert art.manifest["base_model_license"] == CONTRACT.incumbent.license
