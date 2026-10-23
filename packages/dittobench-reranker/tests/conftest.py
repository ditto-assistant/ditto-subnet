"""Tiny, fully synthetic reranker artifacts for tests (no downloads)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dittobench_reranker.contract import CONTRACT_ID

VOCAB_SIZE = 110
NEEDLE_ID = 103


def vocab_text() -> str:
    tokens = ["[PAD]"] + [f"[unused{i}]" for i in range(1, 100)]
    tokens += ["[UNK]", "[CLS]", "[SEP]", "needle", "hay", "where", "is", "the"]
    tokens += [f"[extra{i}]" for i in range(VOCAB_SIZE - len(tokens))]
    return "\n".join(tokens) + "\n"


def build_model(
    needle_weight: float = 1.0,
    output_name: str = "logits",
    extra_domain: str | None = None,
) -> bytes:
    """logits[b] = sum over real tokens of W[token]; W[needle] = needle_weight."""
    from onnx import TensorProto, helper, numpy_helper

    weights = np.zeros((VOCAB_SIZE, 1), dtype=np.float32)
    weights[NEEDLE_ID, 0] = needle_weight
    inits = [
        numpy_helper.from_array(weights, "W"),
        numpy_helper.from_array(np.array([2], dtype=np.int64), "axis2"),
        numpy_helper.from_array(np.array([1], dtype=np.int64), "axis1"),
        numpy_helper.from_array(np.array(0.0, dtype=np.float32), "zero"),
    ]
    nodes = [
        helper.make_node("Gather", ["W", "input_ids"], ["emb"], axis=0),
        helper.make_node("Cast", ["attention_mask"], ["maskf"], to=TensorProto.FLOAT),
        helper.make_node("Unsqueeze", ["maskf", "axis2"], ["mask3"]),
        helper.make_node("Mul", ["emb", "mask3"], ["masked"]),
        helper.make_node("Cast", ["token_type_ids"], ["typesf"], to=TensorProto.FLOAT),
        helper.make_node("Mul", ["typesf", "zero"], ["types0"]),
        helper.make_node("Unsqueeze", ["types0", "axis2"], ["types3"]),
        helper.make_node("Add", ["masked", "types3"], ["summed"]),
        helper.make_node("ReduceSum", ["summed", "axis1"], [output_name], keepdims=0),
    ]
    opsets = [helper.make_opsetid("", 17)]
    if extra_domain:
        nodes.append(
            helper.make_node("Custom", [output_name], ["unused"], domain=extra_domain)
        )
        opsets.append(helper.make_opsetid(extra_domain, 1))
    inputs = [
        helper.make_tensor_value_info(n, TensorProto.INT64, ["batch", "seq"])
        for n in ("input_ids", "attention_mask", "token_type_ids")
    ]
    outputs = [
        helper.make_tensor_value_info(output_name, TensorProto.FLOAT, ["batch", 1])
    ]
    graph = helper.make_graph(nodes, "tiny-reranker", inputs, outputs, inits)
    model = helper.make_model(graph, opset_imports=opsets)
    model.ir_version = 8
    return model.SerializeToString()


def write_artifact(
    directory: Path,
    model: bytes | None = None,
    vocab: str | None = None,
    manifest: dict | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.onnx").write_bytes(
        model if model is not None else build_model()
    )
    (directory / "vocab.txt").write_text(vocab if vocab is not None else vocab_text())
    m = manifest or {
        "contract": CONTRACT_ID,
        "base_model": "none",
        "base_model_license": "mit",
    }
    (directory / "manifest.json").write_text(json.dumps(m))
    return directory


def bundle_bytes(n_pools: int = 6) -> bytes:
    pools = []
    for p in range(n_pools):
        cands = [{"id": f"c{i:02d}", "text": "hay hay"} for i in range(20)]
        # Gold is lexically marked with the needle and sits last in base order.
        cands[18]["text"] = "the needle"
        cands[19]["text"] = "needle is here"
        pools.append(
            {
                "pool_id": f"p{p}",
                "query": "where is the needle",
                "candidates": cands,
                "gold": ["c18", "c19"],
            }
        )
    doc = {
        "schema": "dittobench-rerank-pools-v1",
        "bench_version": 13,
        "run_size": "full",
        "pool_size": 20,
        "sets": [{"seed": 1, "pools": pools}],
    }
    return json.dumps(doc).encode()


@pytest.fixture()
def artifact_dir(tmp_path: Path) -> Path:
    return write_artifact(tmp_path / "artifact")


@pytest.fixture()
def pools_path(tmp_path: Path) -> Path:
    path = tmp_path / "pools.json"
    path.write_bytes(bundle_bytes())
    return path
