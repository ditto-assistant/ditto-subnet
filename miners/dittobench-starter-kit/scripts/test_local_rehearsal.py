from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("local-rehearsal.py")
SPEC = importlib.util.spec_from_file_location("local_rehearsal", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
LOCAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LOCAL
SPEC.loader.exec_module(LOCAL)


class LocalRehearsalTest(unittest.TestCase):
    def test_submit_body_is_explicitly_v8_and_omits_unpinned_seed(self) -> None:
        self.assertEqual(
            LOCAL.submit_body("small", "http://127.0.0.1:8080", None),
            {
                "bench_version": 8,
                "harness_url": "http://127.0.0.1:8080",
                "run_size": "small",
            },
        )

    def test_submit_body_keeps_reproducible_seed(self) -> None:
        body = LOCAL.submit_body("medium", "http://127.0.0.1:8080", 42)
        self.assertEqual(body["seed"], 42)

    def test_process_ports_are_distinct(self) -> None:
        ports = LOCAL.reserve_ports(3)
        self.assertEqual(len(ports), 3)
        self.assertEqual(len(set(ports)), 3)

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


if __name__ == "__main__":
    unittest.main()
