#!/usr/bin/env python3
"""Validate and aggregate the five frozen LongMemEval result files."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
from typing import Any


AGENTS = [
    {
        "rank": 1,
        "name": "whitycatboss",
        "version": 1,
        "dittobench_v6_composite": 0.931983,
        "artifact_sha256": "17dc79bd6608f4bd4783e905db9f4fe12dd04e4753a092b749055b963e8efa3d",
        "image_id": "sha256:3227b1dfcf3b20b0328c86ae36b89d9535232cf14c8b954175294bc0eaa713a4",
    },
    {
        "rank": 2,
        "name": "killer-4",
        "version": 2,
        "dittobench_v6_composite": 0.931597,
        "artifact_sha256": "92f68b01c2d82b2333b50654bec6b9e7eac2fb02a9eb83dabc605088908ee504",
        "image_id": "sha256:95bf026723697ecbc9dbb8a90ab097f72a9b72d7a924798c49346d2b2ef86976",
    },
    {
        "rank": 3,
        "name": "ditto-max-v1",
        "version": 1,
        "dittobench_v6_composite": 0.916441,
        "artifact_sha256": "07bb32981c782fc51c0f910d26e071814be53044183d96f4f1bc512c69a6207e",
        "image_id": "sha256:dcc07d39255641ff352ba45198757b919eb68c691d6b28ef515fe5be6057b6da",
    },
    {
        "rank": 4,
        "name": "avadakedavra",
        "version": 1,
        "dittobench_v6_composite": 0.911062,
        "artifact_sha256": "6d8b7c434808b9c4bab4fbe57d91c3fcf977c6fba643c52f8375afd9b30c200a",
        "image_id": "sha256:e7a4fe3bb1f47c017aa36a1c40d5bc1ad2676be20f09c7601884a84dbab5d4c5",
    },
    {
        "rank": 5,
        "name": "zeus_v12",
        "version": 1,
        "dittobench_v6_composite": 0.902444,
        "artifact_sha256": "6dc13e83976fa679efc1e2968856b55a28eecb26fc98dc9ca030815e9d5d7205",
        "image_id": "sha256:ac4e0eb2f4bf83165d64a83a4774dd93febd9ed8f14fc4f3b95dc8dc8178b31b",
    },
]
JUDGE_MODEL = "gpt-4o-2024-08-06"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarize_agent(
    hypotheses: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    references: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected = set(references)
    hypothesis_ids = [row["question_id"] for row in hypotheses]
    evaluation_ids = [row["question_id"] for row in evaluations]
    for label, ids in (("hypotheses", hypothesis_ids), ("evaluations", evaluation_ids)):
        if len(ids) != len(expected) or len(set(ids)) != len(ids) or set(ids) != expected:
            raise ValueError(f"{label} do not contain exactly the reference question IDs")
    empty_answer_ids = {
        row["question_id"] for row in hypotheses if not str(row.get("hypothesis", "")).strip()
    }

    by_category: dict[str, list[bool]] = collections.defaultdict(list)
    labels: list[bool] = []
    empty_answers_judged_correct = 0
    for row in evaluations:
        autoeval = row.get("autoeval_label") or {}
        if autoeval.get("model") != JUDGE_MODEL or not isinstance(autoeval.get("label"), bool):
            raise ValueError("evaluation is not from the frozen official judge")
        question_id = row["question_id"]
        label = autoeval["label"]
        if question_id in empty_answer_ids and label:
            empty_answers_judged_correct += 1
        labels.append(label)
        by_category[references[question_id]["question_type"]].append(label)

    abstention = [
        row["autoeval_label"]["label"]
        for row in evaluations
        if row["question_id"].endswith("_abs")
    ]
    return {
        "n": len(labels),
        "correct": sum(labels),
        "accuracy": round(sum(labels) / len(labels), 6),
        "empty_answers": len(empty_answer_ids),
        "empty_answers_judged_correct": empty_answers_judged_correct,
        "abstention": {"n": len(abstention), "accuracy": round(sum(abstention) / len(abstention), 6)},
        "per_category": {
            category: {"n": len(values), "accuracy": round(sum(values) / len(values), 6)}
            for category, values in sorted(by_category.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--condition",
        default="longmemeval-s-cleaned-native-dittobench-memory-v1",
    )
    parser.add_argument("--answer-model-id", default="qwen/qwen3-32b")
    parser.add_argument("--answer-model-provider", default="openrouter")
    parser.add_argument(
        "--answer-model-profile",
        default="openrouter-nebius-qwen3-32b-no-think-v1",
    )
    parser.add_argument("--embedding-model-id", default="embeddinggemma")
    parser.add_argument("--embedding-model-provider", default="ollama")
    parser.add_argument("--embedding-model-profile", default="local-embeddinggemma-768")
    parser.add_argument("--embedding-dimensions", type=int, default=768)
    parser.add_argument(
        "--run-telemetry",
        help="optional aggregate-only JSON captured from the trusted proxies",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    references_list = json.loads(dataset_path.read_text())
    references = {row["question_id"]: row for row in references_list}
    results_dir = Path(args.results_dir)
    agents = []
    for agent in AGENTS:
        rank = agent["rank"]
        hypothesis_path = results_dir / f"agent-{rank}.jsonl"
        evaluation_path = results_dir / f"agent-{rank}.jsonl.eval-results-gpt-4o"
        aggregate = summarize_agent(read_jsonl(hypothesis_path), read_jsonl(evaluation_path), references)
        agents.append(
            {
                **agent,
                **aggregate,
                "hypotheses_sha256": sha256(hypothesis_path),
                "evaluations_sha256": sha256(evaluation_path),
            }
        )

    output = {
        "schema_version": 1,
        "condition": args.condition,
        "leaderboard_snapshot_at": "2026-07-23T05:37:19.615917Z",
        "leaderboard_bench_version": 6,
        "dataset": {
            "name": "longmemeval_s_cleaned.json",
            "questions": len(references),
            "sha256": sha256(dataset_path),
            "huggingface_revision": "98d7416c24c778c2fee6e6f3006e7a073259d48f",
            "longmemeval_revision": "9e0b455f4ef0e2ab8f2e582289761153549043fc",
        },
        "answer_model": {
            "id": args.answer_model_id,
            "provider": args.answer_model_provider,
            "profile_revision": args.answer_model_profile,
        },
        "embedding_model": {
            "id": args.embedding_model_id,
            "provider": args.embedding_model_provider,
            "profile_revision": args.embedding_model_profile,
            "dimensions": args.embedding_dimensions,
        },
        "public_harness_revision": "c3caa8e2c19f8a41a0610b9f7db774f97643dd9c",
        "judge": {
            "official_model": JUDGE_MODEL,
            "openrouter_model": "openai/gpt-4o-2024-08-06",
            "profile_revision": "longmemeval-official-gpt4o-openrouter-v1",
        },
        "agents": agents,
    }
    if args.run_telemetry:
        output["run_telemetry"] = json.loads(Path(args.run_telemetry).read_text())
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
