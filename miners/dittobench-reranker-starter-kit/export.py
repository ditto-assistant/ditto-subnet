#!/usr/bin/env python3
"""Export a Hugging Face cross-encoder into a Season 2 reranker artifact.

    python export.py cross-encoder/ms-marco-MiniLM-L2-v2 out/ --license apache-2.0
    python export.py ./my-finetuned-model out/ --base-model cross-encoder/... \
        --license apache-2.0

Writes ``out/model.onnx`` (dynamic INT8 by default, like the product's
``crossencoder/export.sh``), ``out/vocab.txt`` and ``out/manifest.json``, then
runs the season's static validation on the result.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("model", help="Hugging Face id or local directory")
    parser.add_argument("out", type=Path)
    parser.add_argument(
        "--base-model",
        help="model this one derives from (defaults to the model argument)",
    )
    parser.add_argument("--license", required=True, help="SPDX id of the base model")
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()

    from onnxruntime.quantization import QuantType, quantize_dynamic
    from optimum.onnxruntime import ORTModelForSequenceClassification
    from transformers import AutoTokenizer

    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        model = ORTModelForSequenceClassification.from_pretrained(
            args.model, export=True
        )
        model.save_pretrained(work)
        AutoTokenizer.from_pretrained(args.model).save_pretrained(work)
        if not (work / "vocab.txt").exists():
            print(
                "error: the tokenizer has no vocab.txt; the season needs an "
                "uncased BERT WordPiece vocabulary",
                file=sys.stderr,
            )
            return 2
        shutil.copy(work / "vocab.txt", args.out / "vocab.txt")
        if args.no_quantize:
            shutil.copy(work / "model.onnx", args.out / "model.onnx")
        else:
            quantize_dynamic(
                str(work / "model.onnx"),
                str(args.out / "model.onnx"),
                weight_type=QuantType.QInt8,
            )

    manifest = {
        "contract": "reranker-season-v1",
        "base_model": args.base_model or args.model,
        "base_model_license": args.license.lower(),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    from dittobench_reranker.artifact import ArtifactError, load_artifact

    try:
        art = load_artifact(args.out)
    except ArtifactError as exc:
        print(f"error: exported artifact is invalid: {exc}", file=sys.stderr)
        return 2
    print(f"{args.out}: valid, artifact sha256 {art.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
