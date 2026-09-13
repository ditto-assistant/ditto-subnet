#!/usr/bin/env python3
"""Offline evidence audit; no training, provider calls or checkpoint code execution.

The checkpoint reader is deliberately narrow: only plain float32 contiguous
tensor descriptors and OrderedDict are accepted. It does not import torch or
invoke torch.load, model code, or the backend exporter.
"""

import argparse
import ast
from collections import Counter, OrderedDict
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import re
import struct
import sys
import zipfile

from longmemeval_adapter import iter_json_array

DATASET_SHA = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
RETEST_MODEL_SHA = "46d34091333706c841b6e0f6b35b2bf9f5f89ac0fca692720ce9c22cc795138c"
EXPORT_NAMES = tuple(f"{layer}.{part}" for layer in
                     ("embed_fc1", "embed_ln1", "embed_fc2", "embed_ln2", "fusion_fc", "fusion_ln", "output_fc")
                     for part in ("weight", "bias")) + ("output_scale.weight",)


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def default_split(source, count):
    functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "split"]
    require(len(functions) == 1, "training split function missing or ambiguous")
    function = functions[0]
    defaults = {arg.arg: ast.literal_eval(value) for arg, value in
                zip(function.args.args[-len(function.args.defaults):], function.args.defaults)}
    require(defaults == {"val_frac": 0.1, "seed": 1234}, "training defaults changed; audit split implementation before proceeding")
    return {"validation_fraction": defaults["val_frac"], "shuffle_seed": defaults["seed"],
            "training_rows": count - max(1, int(count * defaults["val_frac"])),
            "validation_rows": max(1, int(count * defaults["val_frac"])), "historical_partition_proven": False}


def unique_object(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Storage:
    key: str
    count: int


@dataclass(frozen=True)
class Tensor:
    storage: Storage
    offset: int
    shape: tuple
    stride: tuple


def rebuild_tensor(storage, offset, shape, stride, requires_grad, hooks):
    require(isinstance(storage, Storage), "invalid tensor storage")
    require(type(offset) is int and offset >= 0, "invalid tensor offset")
    require(isinstance(shape, tuple) and shape and len(shape) <= 4 and
            all(type(d) is int and d > 0 for d in shape), "invalid tensor shape")
    require(isinstance(stride, tuple) and len(stride) == len(shape), "invalid tensor stride")
    require(type(requires_grad) is bool and not hooks, "unsupported tensor flags/hooks")
    expected = 1
    for size, step in zip(reversed(shape), reversed(stride)):
        require(type(step) is int and step == expected, "only contiguous tensors are supported")
        expected *= size
    require(offset + expected <= storage.count, "tensor exceeds storage")
    return Tensor(storage, offset, shape, stride)


class DescriptorUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        allowed = {("collections", "OrderedDict"): OrderedDict,
                   ("torch", "FloatStorage"): "float32",
                   ("torch._utils", "_rebuild_tensor_v2"): rebuild_tensor}
        require((module, name) in allowed, "unsupported checkpoint global")
        return allowed[(module, name)]

    def persistent_load(self, value):
        require(isinstance(value, tuple) and len(value) == 5, "unsupported storage reference")
        kind, dtype, key, location, count = value
        require(kind == "storage" and dtype == "float32" and location == "cpu", "unsupported storage type/device")
        require(isinstance(key, str) and re.fullmatch(r"\d+", key), "invalid storage key")
        require(type(count) is int and 0 < count < 2_000_000, "invalid storage size")
        return Storage(key, count)


def checkpoint_tensors(path):
    require(path.stat().st_size < 16_000_000, "checkpoint exceeds audit size limit")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and len(names) < 64, "invalid checkpoint members")
        require(sum(info.file_size for info in archive.infolist()) < 16_000_000, "expanded checkpoint exceeds limit")
        candidates = [n for n in names if n.endswith("/data.pkl")]
        require(len(candidates) == 1, "checkpoint must contain one descriptor pickle")
        prefix = candidates[0].removesuffix("data.pkl")
        require(archive.read(prefix + "byteorder") == b"little", "only little-endian checkpoint is supported")
        blob = DescriptorUnpickler(io.BytesIO(archive.read(candidates[0]))).load()
        require(isinstance(blob, dict) and isinstance(blob.get("state_dict"), dict), "checkpoint state dictionary missing")
        result = {}
        for name, descriptor in blob["state_dict"].items():
            require(isinstance(name, str) and isinstance(descriptor, Tensor), "unsupported checkpoint tensor")
            raw = archive.read(prefix + "data/" + descriptor.storage.key)
            require(len(raw) == 4 * descriptor.storage.count, "checkpoint storage length differs")
            count = math.prod(descriptor.shape)
            result[name] = (descriptor.shape, raw[4 * descriptor.offset:4 * (descriptor.offset + count)])
        metadata = {k: blob.get(k) for k in ("aux_dim", "num_weights", "with_scale", "val_ndcg10", "epoch")}
        return result, metadata, sorted(k for k in blob if k != "state_dict")


def export_bytes(tensors):
    require(set(tensors) == set(EXPORT_NAMES[:-1]) | {"output_scale"}, "checkpoint tensor names differ from frozen exporter")
    output = bytearray(struct.pack("<H", len(EXPORT_NAMES)))
    for name in EXPORT_NAMES:
        shape, raw = tensors["output_scale" if name == "output_scale.weight" else name]
        require(len(raw) == math.prod(shape) * 4, "tensor byte count differs")
        encoded = name.encode()
        output.extend(struct.pack("<H", len(encoded)) + encoded + struct.pack("<B", len(shape)))
        output.extend(struct.pack("<" + "I" * len(shape), *shape))
        output.extend(raw)
    return bytes(output)


def overlap(training, dataset):
    ids = [row["question_id"] for row in training]
    require(len(ids) == len(set(ids)), "duplicate training question IDs")
    seen = set(ids)
    matched = seen & dataset.keys()
    queries_equal = sum(row.get("query") == dataset[row["question_id"]]["question"]
                        for row in training if row["question_id"] in matched)
    categories = {}
    for category in sorted({d["question_type"] for d in dataset.values()}):
        members = {qid for qid, d in dataset.items() if d["question_type"] == category}
        categories[category] = {"evaluation_questions": len(members), "training_input_overlap": len(members & seen)}
    unmatched = set(dataset) - seen
    trained_sessions = {s for qid in matched for s in dataset[qid]["session_ids"]}
    unmatched_sessions = {s for qid in unmatched for s in dataset[qid]["session_ids"]}
    return {"training_input_rows": len(training), "training_unique_question_ids": len(seen),
            "evaluation_unique_question_ids": len(dataset), "overlap_question_ids": len(matched),
            "training_question_ids_absent_from_evaluation": len(seen - dataset.keys()),
            "exact_query_text_matches_among_overlap": queries_equal,
            "evaluation_question_ids_absent_from_training_input": len(unmatched),
            "by_question_type": categories,
            "training_request_paths": dict(Counter(r.get("request_path", "") for r in training)),
            "candidate_grade_counts": dict(sorted(Counter(str(c.get("grade")) for r in training for c in r.get("candidates", [])).items())),
            "training_input_abstention_id_count": sum(q.endswith("_abs") for q in seen),
            "unmatched_abstention_id_count": sum(q.endswith("_abs") for q in unmatched),
            "unmatched_evaluation_histories_sharing_source_sessions_with_matched_evaluation_histories": sum(
                bool(set(dataset[qid]["session_ids"]) & trained_sessions) for qid in unmatched),
            "shared_source_session_ids_across_matched_unmatched_evaluation_histories": len(trained_sessions & unmatched_sessions),
            "session_scope_limitation": "These are full evaluation haystack session IDs, not proof of exact oracle training-session inclusion. A question-ID-disjoint remainder is not automatically history-disjoint."}


def audit(backend, dataset_path):
    require(file_sha(dataset_path) == DATASET_SHA, "dataset digest differs from pinned cleaned LongMemEval-S")
    dataset = {}
    for row in iter_json_array(dataset_path):
        qid = row["question_id"]
        require(qid not in dataset, "duplicate evaluation question ID")
        dataset[qid] = {"question": row["question"], "question_type": row["question_type"],
                        "session_ids": row["haystack_session_ids"]}
    require(len(dataset) == 500, "evaluation must contain exactly 500 questions")
    root = backend / "pkg/services/retrieval"
    training = [json.loads(line, object_pairs_hook=unique_object) for line in (root / "training/training_set.jsonl").read_text().splitlines() if line.strip()]
    tensors, metadata, metadata_keys = checkpoint_tensors(root / "training/model.pt")
    exported = export_bytes(tensors)
    model = (root / "model.bin").read_bytes()
    metrics = json.loads((root / "training/metrics.json").read_text())
    best = max(metrics, key=lambda r: r["val_ndcg10"])
    paths = ("model.bin", "model.meta.json", "training/model.pt", "training/training_set.jsonl", "training/metrics.json",
             "training/question_embeddings.npz", "training/train.py", "training/model.py", "training/export.py", "training/format.py", "training/synthesize_from_oracle.py")
    return {"schema_version": 1, "dataset_sha256": DATASET_SHA,
            "artifact_sha256": {name: file_sha(root / name) for name in paths},
            "overlap": overlap(training, dataset),
            "checkpoint_export": {"exact_model_bin_byte_match": exported == model, "reconstructed_export_sha256": sha(exported),
                                  "model_bin_sha256": sha(model), "tensor_count": len(tensors),
                                  "matches_frozen_retest_model_sha256": sha(model) == RETEST_MODEL_SHA,
                                  "float32_parameter_count_including_sentinel": sum(math.prod(shape) for shape, _ in tensors.values()),
                                  "checkpoint_metadata": metadata, "checkpoint_top_level_metadata_keys": metadata_keys,
                                  "metrics_best_epoch": best["epoch"], "metrics_best_val_ndcg10": best["val_ndcg10"],
                                  "checkpoint_best_epoch_and_metric_match": metadata["epoch"] == best["epoch"] and metadata["val_ndcg10"] == best["val_ndcg10"],
                                  "training_run_input_hash_or_split_ids_attested": False},
            "checked_in_train_script_default_split": default_split((root / "training/train.py").read_text(), len(training)),
            "limitations": ["This audits Ditto's small retrieval-weight MLP, not Luna's provider training corpus.",
                            "Exact export parity proves checkpoint-to-model.bin identity, not a replayed or signed training run.",
                            "Historical report claims 80/20, while committed training code defaults 90/10; input/split provenance was not embedded in the checkpoint.",
                            "No model training, tuning, provider calls, database reads/writes or raw question output."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "output exists; preserve previous evidence")
    result = audit(args.backend, args.dataset)
    require(result["checkpoint_export"]["exact_model_bin_byte_match"], "checkpoint export does not match embedded model")
    require(result["checkpoint_export"]["matches_frozen_retest_model_sha256"], "model differs from frozen retest weights")
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"overlap": result["overlap"]["overlap_question_ids"], "model_bin_export_match": True}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, pickle.UnpicklingError, zipfile.BadZipFile) as exc:
        print(f"lineage audit refused: {exc}", file=sys.stderr)
        sys.exit(1)
