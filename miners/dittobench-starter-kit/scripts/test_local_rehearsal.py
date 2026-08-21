from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).with_name("local-rehearsal.py")
SPEC = importlib.util.spec_from_file_location("local_rehearsal", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
LOCAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LOCAL
SPEC.loader.exec_module(LOCAL)


class LocalRehearsalTest(unittest.TestCase):
    def test_submit_body_defaults_to_live_scoring_version(self) -> None:
        self.assertEqual(
            LOCAL.submit_body("small", "http://127.0.0.1:8080", None),
            {
                "bench_version": LOCAL.LIVE_SCORING_BENCH_VERSION,
                "harness_url": "http://127.0.0.1:8080",
                "run_size": "small",
            },
        )
        self.assertEqual(LOCAL.LIVE_SCORING_BENCH_VERSION, 11)

    def test_submit_body_can_pin_an_older_contract(self) -> None:
        body = LOCAL.submit_body("small", "http://127.0.0.1:8080", None, 9)
        self.assertEqual(body["bench_version"], 9)

    def test_submit_body_keeps_reproducible_seed(self) -> None:
        body = LOCAL.submit_body("medium", "http://127.0.0.1:8080", 42)
        self.assertEqual(body["seed"], 42)

    def test_process_ports_are_distinct(self) -> None:
        ports = LOCAL.reserve_ports(3)
        self.assertEqual(len(ports), 3)
        self.assertEqual(len(set(ports)), 3)

    def test_report_write_is_atomic_private_and_replaceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "nested" / "report.json"
            LOCAL.write_report(report, {"bench_version": 9})
            self.assertEqual(json.loads(report.read_text()), {"bench_version": 9})
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)

            LOCAL.write_report(report, {"bench_version": 9, "longmemeval": {}})
            self.assertIn("longmemeval", json.loads(report.read_text()))
            self.assertEqual(list(report.parent.glob("*.tmp-*")), [])

    def test_summary_exposes_observed_and_capped_tool_counts(self) -> None:
        summary = LOCAL.format_summary(
            {
                "run_id": "run-1",
                "status": "done",
                "report": {
                    "seed": 42,
                    "composite": 0.75,
                    "tool_mean": 1,
                    "memory_mean": 0.5,
                    "details": {
                        "dataset_sha256": "a" * 64,
                        "observed_tool_cases": 5,
                        "capped_tool_cases": 1,
                    },
                },
            }
        )
        self.assertIn("observed_tool_cases: 5", summary)
        self.assertIn("capped_tool_cases:   1", summary)
        self.assertIn("not submission", summary)
        self.assertIn("name-only scorer", summary)
        self.assertIn("local rehearsal", summary)

    def test_log_redaction_covers_openrouter_and_bearer_tokens(self) -> None:
        redacted = LOCAL.redact(
            "key=sk-or-v1-example-secret Authorization: Bearer validator-secret"
        )
        self.assertNotIn("example-secret", redacted)
        self.assertNotIn("validator-secret", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 2)

    def test_timeout_must_be_positive(self) -> None:
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--timeout", "0"])

    def test_default_and_rejected_bench_versions(self) -> None:
        self.assertEqual(
            LOCAL.parse_args([]).bench_version, LOCAL.LIVE_SCORING_BENCH_VERSION
        )
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--bench-version", "7"])
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--bench-version", "13"])

    def test_longmem_limit_requires_longmem_flag(self) -> None:
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--longmem-limit", "1"])

    def test_longmem_bounds_are_fail_closed(self) -> None:
        for argv in (
            ["--longmem-eval", "--longmem-limit", "0"],
            ["--longmem-eval", "--longmem-limit", "501"],
            ["--longmem-eval", "--longmem-shards", "0"],
            ["--longmem-eval", "--longmem-shards", "11"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                LOCAL.parse_args(argv)

    def test_longmem_full_and_partial_labels_are_distinct(self) -> None:
        full = LOCAL.format_longmem_summary(
            {
                "official_full_condition": True,
                "condition": LOCAL.LONGMEM_CONDITION,
                "correct": 277,
                "n": 500,
                "accuracy": 0.554,
                "abstention_correct": 26,
                "abstention_n": 30,
            }
        )
        partial = full.replace("official full condition", "partial practice")
        self.assertIn("official full condition", full)
        self.assertIn("separate offline adapter score", full)
        self.assertNotEqual(full, partial)

    def test_fresh_longmem_command_does_not_request_resume_rebalancing(self) -> None:
        args = LOCAL.parse_args(["--longmem-eval", "--longmem-limit", "1"])
        command = LOCAL.longmem_adapter_command(
            args=args,
            kit_dir=Path("/tmp/starter"),
            dataset=Path("/tmp/dataset.json"),
            harness_urls=["http://127.0.0.1:18001"],
            hypotheses=Path("/tmp/hypotheses.jsonl"),
            manifest=Path("/tmp/manifest.json"),
            user_id_namespace="isolated-practice",
        )
        self.assertNotIn("--resume", command)
        self.assertNotIn("--rebalance-pending", command)
        self.assertEqual(
            command[command.index("--bench-version") + 1],
            str(LOCAL.LIVE_SCORING_BENCH_VERSION),
        )
        self.assertEqual(command[command.index("--limit") + 1], "1")

    def test_summarize_longmem_requires_exact_unique_cardinality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eval.jsonl"
            row = {
                "question_id": "q-1",
                "autoeval_label": {
                    "model": "gpt-4o-2024-08-06",
                    "label": True,
                },
            }
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            with self.assertRaisesRegex(LOCAL.RehearsalError, "unique rows"):
                LOCAL.summarize_longmem(path, limit=2, bench_version=11)

    def test_summarize_longmem_pins_judge_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eval.jsonl"
            rows = [
                {
                    "question_id": "q-1",
                    "autoeval_label": {
                        "model": "gpt-4o-2024-08-06",
                        "label": True,
                    },
                },
                {
                    "question_id": "q-2_abs",
                    "autoeval_label": {
                        "model": "gpt-4o-2024-08-06",
                        "label": False,
                    },
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = LOCAL.summarize_longmem(path, limit=2, bench_version=11)
            self.assertEqual(result["bench_version"], 11)
            self.assertEqual(result["accuracy"], 0.5)
            self.assertEqual(result["abstention_n"], 1)
            self.assertEqual(result["abstention_correct"], 0)
            self.assertFalse(result["official_full_condition"])
            self.assertEqual(result["dataset_sha256"], LOCAL.LONGMEM_DATASET_SHA256)
            self.assertEqual(result["condition"], LOCAL.LONGMEM_CONDITION)

    def test_summarize_longmem_rejects_wrong_judge_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "eval.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "question_id": "q-1",
                        "autoeval_label": {"model": "gpt-4o-mini", "label": True},
                    }
                )
                + "\n"
            )
            with self.assertRaisesRegex(LOCAL.RehearsalError, "pinned official judge"):
                LOCAL.summarize_longmem(path, limit=1, bench_version=11)

    def test_build_environment_drops_provider_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "secret", "CHUTES_API_KEY": "secret-2"},
        ):
            environment = LOCAL.sanitized_build_env()
        self.assertNotIn("OPENROUTER_API_KEY", environment)
        self.assertNotIn("CHUTES_API_KEY", environment)

    def test_judge_git_context_has_advertised_ref_and_full_checksum(self) -> None:
        context = LOCAL.longmem_judge_context()
        self.assertIn("ref=refs/heads/main", context)
        self.assertIn(f"checksum={LOCAL.LONGMEM_SOURCE_REVISION}", context)
        self.assertEqual(len(LOCAL.LONGMEM_SOURCE_REVISION), 40)

    def test_scorer_environment_uses_private_local_evidence_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "secret", "CHUTES_API_KEY": "secret-2"},
            ):
                environment = LOCAL.scorer_environment(root, 18473)

            private_dir = Path(environment["DITTOBENCH_PRIVATE_ARTIFACT_DIR"])
            artifact_dir = Path(environment["DITTOBENCH_ARTIFACT_DIR"])
            self.assertEqual(private_dir, root / "private-projections")
            self.assertEqual(artifact_dir, root / "artifacts")
            self.assertEqual(private_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(artifact_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(environment["DITTOBENCH_BROKER_PORT"], "18473")
            self.assertNotIn("OPENROUTER_API_KEY", environment)
            self.assertNotIn("CHUTES_API_KEY", environment)

    def test_cached_dataset_is_used_only_after_digest_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "dataset.json"
            cached.write_bytes(b"verified")
            digest = LOCAL.hashlib.sha256(b"verified").hexdigest()
            with (
                patch.object(LOCAL, "longmem_dataset_cache_path", return_value=cached),
                patch.object(LOCAL, "LONGMEM_DATASET_SHA256", digest),
                patch.object(LOCAL.urllib.request, "urlopen") as urlopen,
            ):
                self.assertEqual(LOCAL.ensure_longmem_dataset(), cached)
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
