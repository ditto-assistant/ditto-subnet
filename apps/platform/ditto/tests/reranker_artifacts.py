"""Tiny, fully synthetic Season 2 reranker artifacts for Platform tests.

Copied from ``packages/dittobench-reranker/tests/conftest.py`` rather than
imported, so the two test trees stay independent. Nothing is downloaded.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile

import numpy as np
from dittobench_reranker.contract import CONTRACT_ID

VOCAB_SIZE = 110
NEEDLE_ID = 103


def vocab_text() -> str:
    tokens = ["[PAD]"] + [f"[unused{i}]" for i in range(1, 100)]
    tokens += ["[UNK]", "[CLS]", "[SEP]", "needle", "hay", "where", "is", "the"]
    tokens += [f"[extra{i}]" for i in range(VOCAB_SIZE - len(tokens))]
    return "\n".join(tokens) + "\n"


def build_model(needle_weight: float = 1.0, output_name: str = "logits") -> bytes:
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
    inputs = [
        helper.make_tensor_value_info(n, TensorProto.INT64, ["batch", "seq"])
        for n in ("input_ids", "attention_mask", "token_type_ids")
    ]
    outputs = [
        helper.make_tensor_value_info(output_name, TensorProto.FLOAT, ["batch", 1])
    ]
    graph = helper.make_graph(nodes, "tiny-reranker", inputs, outputs, inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    return model.SerializeToString()


def manifest(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "contract": CONTRACT_ID,
        "base_model": "none",
        "base_model_license": "mit",
    }
    base.update(overrides)
    return base


def tarball(
    files: dict[str, bytes] | None = None,
    *,
    model: bytes | None = None,
    vocab: str | None = None,
    manifest_doc: dict[str, object] | None = None,
) -> bytes:
    """A gzipped tar of the three artifact files (or exactly ``files``)."""
    if files is None:
        files = {
            "model.onnx": model if model is not None else build_model(),
            "vocab.txt": (vocab if vocab is not None else vocab_text()).encode(),
            "manifest.json": json.dumps(manifest_doc or manifest()).encode(),
        }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    # mtime=0: identical inputs give identical bytes (and stable test ids).
    return gzip.compress(buf.getvalue(), mtime=0)
