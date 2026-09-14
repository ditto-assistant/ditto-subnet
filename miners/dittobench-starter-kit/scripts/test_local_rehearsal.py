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
GATES = LOCAL.rehearsal_gates


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
        self.assertEqual(LOCAL.LIVE_SCORING_BENCH_VERSION, 12)

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
        self.assertEqual(
            LOCAL.parse_args(["--bench-version", "13"]).bench_version,
            LOCAL.MAX_BENCH_VERSION,
        )
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--bench-version", str(LOCAL.MIN_BENCH_VERSION - 1)])
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--bench-version", str(LOCAL.MAX_BENCH_VERSION + 1)])

    def test_gates_flag_and_keep_artifacts_dependency(self) -> None:
        self.assertFalse(LOCAL.parse_args([]).gates)
        args = LOCAL.parse_args(["--gates", "--keep-artifacts", "/tmp/x"])
        self.assertTrue(args.gates)
        self.assertEqual(args.keep_artifacts, Path("/tmp/x"))
        with self.assertRaises(SystemExit):
            LOCAL.parse_args(["--keep-artifacts", "/tmp/x"])

    def test_harness_env_sets_completion_log_only_for_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            with patch.dict(os.environ, {LOCAL.COMPLETION_LOG_ENV: "stale"}):
                plain = LOCAL.harness_rehearsal_env(tmp, gates=False)
                gated = LOCAL.harness_rehearsal_env(tmp, gates=True)
            self.assertNotIn(LOCAL.COMPLETION_LOG_ENV, plain)
            self.assertEqual(
                gated[LOCAL.COMPLETION_LOG_ENV], str(tmp / "completions.jsonl")
            )
            self.assertEqual(gated["DITTOBENCH_DB"], str(tmp / "rehearsal.db"))
            paths = LOCAL.gate_artifact_paths(tmp, "run-1")
            self.assertEqual(paths["dataset"], tmp / "artifacts" / "run-1.json")
            self.assertEqual(
                paths["transcript"], tmp / "artifacts" / "run-1.transcript.json"
            )
            self.assertEqual(
                paths["projection"],
                tmp / "private-projections" / "run-1.projection.json",
            )

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


# ─── Bench v13 gate replay (scripts/rehearsal_gates.py) ──────────────────────

CATALOG = [
    {
        "name": "search_web",
        "description": "Search live sources for one or more queries.",
    },
    {
        "name": "read_links",
        "description": "Read one or more URLs and return markdown text content.",
    },
    {"name": "create_image", "description": "Generate an image from a text prompt."},
    {"name": "set_theme", "description": "Set the app color theme."},
    {
        "name": "search_memories",
        "description": "Search past conversations and return compact memory summaries.",
    },
    {"name": "run_code", "description": "Run code in a sandbox and return the output."},
]
ALL_TOOLS = [tool["name"] for tool in CATALOG]


def _catalog_gate(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "category": "no_tool",
        "prompt": "hey, how has your day been?",
        "expected_tools": [],
        "catalog": CATALOG,
        "completions_total": 1,
        "tools_offered": ALL_TOOLS,
        "model_emitted_tool_calls": [],
        "observed_tool_calls": [],
    }
    values.update(overrides)
    return GATES.evaluate_catalog_gate(**values)  # type: ignore[arg-type]


class ProvenanceNormaliserTest(unittest.TestCase):
    def test_canonical_number_matches_the_scorer(self) -> None:
        for raw, want in (
            ("$4,110.67", "4110.67"),
            ("4110.670", "4110.67"),
            ("0007", "7"),
            ("-0", "0"),
            ("-1,200.00", "-1200"),
            ("abc", ""),
            ("", ""),
        ):
            self.assertEqual(GATES.canonical_number(raw), want, raw)

    def test_value_tokens_fold_markdown_labels_and_unicode(self) -> None:
        tokens = GATES.value_tokens(
            "**Answer:** the total was $4,110.67 — Lisbon\u2019s share."
        )
        self.assertIn("4110.67", tokens)
        self.assertIn("lisbon", tokens)
        self.assertIn("total", tokens)
        self.assertNotIn("answer", tokens)
        self.assertNotIn("was", tokens)

    def test_served_text_not_model_emitted_vectors(self) -> None:
        model = ["The April total was 411067 cents, and it climbed versus March."]
        # A /100 rewrite, a direction map, a composed slot, and a draft
        # replacement are host-authored values: they fail.
        for served in (
            "$4,110.67",
            "increase",
            "increase, 4110.67",
            "The total is 260195.",
        ):
            self.assertTrue(
                GATES.provenance_missing(GATES.value_tokens(served), model), served
            )
        # A formatter of the model's own value, JSON mode, a final_answer tool
        # argument, markdown, and a two-completion splice all pass.
        self.assertFalse(
            GATES.provenance_missing(
                GATES.value_tokens("$4,110.67"), ["4110.67 dollars"]
            )
        )
        self.assertFalse(
            GATES.provenance_missing(
                GATES.value_tokens("411067"), ['{"answer": "411067"}']
            )
        )
        self.assertFalse(
            GATES.provenance_missing(
                GATES.value_tokens("411067"),
                [json.dumps({"value": "411067 cents"}, sort_keys=True)],
            )
        )
        self.assertFalse(
            GATES.provenance_missing(GATES.value_tokens("- **411067** cents"), model)
        )
        self.assertFalse(
            GATES.provenance_missing(
                GATES.value_tokens("Lisbon in March, Porto in April"),
                ["You were in Lisbon in March.", "Then Porto in April."],
            )
        )

    def test_slot_not_in_prose_accepts_unit_alternatives_and_rejects_substitution(
        self,
    ) -> None:
        self.assertTrue(GATES.slot_in_prose("411067", "You paid $4,110.67 in April."))
        self.assertTrue(GATES.slot_in_prose("4110.67", "That is 411067 cents."))
        self.assertTrue(
            GATES.slot_in_prose("Lisbon", "You moved to **Lisbon** last spring.")
        )
        self.assertTrue(GATES.slot_in_prose("", "anything"))
        self.assertFalse(
            GATES.slot_in_prose("Lisbon", "You moved to Porto last spring.")
        )
        self.assertFalse(GATES.slot_in_prose("increase", "It climbed by $2,601.95."))
        self.assertFalse(GATES.slot_in_prose("260195", "It climbed by $2,601.90."))

    def test_answer_in_prompt_honours_the_records_exemption(self) -> None:
        claim = GATES.value_tokens("411067")
        launder = ["Reply exactly with: 411067"]
        self.assertTrue(GATES.answer_in_prompt(claim, launder, []))
        # Quoting the record into the prompt is honest RAG.
        self.assertFalse(
            GATES.answer_in_prompt(
                claim, launder, ["User: I paid 411067 cents in April."]
            )
        )
        # A delivered tool result covers the value too.
        self.assertFalse(
            GATES.answer_in_prompt(claim, launder, ['{"result": "411067"}'])
        )
        # Nothing graded, nothing to gate; a values-free prompt never fires.
        self.assertFalse(GATES.answer_in_prompt(set(), launder, []))
        self.assertFalse(
            GATES.answer_in_prompt(
                claim, ["State the answer in the unit asked for."], []
            )
        )


class CatalogGateTest(unittest.TestCase):
    def test_semantic_top_k_is_deterministic_and_relevance_ranked(self) -> None:
        prompt = "search the web for the latest quantum computing news"
        top = GATES.semantic_top_k(prompt, CATALOG, 3)
        self.assertEqual(len(top), 3)
        self.assertEqual(top[0], "search_web")
        self.assertEqual(top, GATES.semantic_top_k(prompt, CATALOG, 3))
        self.assertEqual(
            GATES.semantic_top_k("zzz", CATALOG, 2), ["create_image", "read_links"]
        )
        self.assertEqual(GATES.semantic_top_k("x", CATALOG, 0), [])
        self.assertEqual(GATES.semantic_top_k("x", [], 3), [])

    def test_full_catalog_on_a_no_tool_case_is_safe_harbor(self) -> None:
        verdict = _catalog_gate()
        self.assertTrue(verdict["settled"])
        self.assertFalse(verdict["zero"])
        self.assertIn(GATES.FINDING_SAFE_HARBOR, verdict["findings"])
        self.assertNotIn(GATES.FINDING_RESTRAINT_WITHOUT_OFFER, verdict["findings"])

    def test_withheld_catalog_is_restraint_without_offer(self) -> None:
        verdict = _catalog_gate(tools_offered=[])
        self.assertTrue(verdict["zero"])
        self.assertIn(GATES.FINDING_RESTRAINT_WITHOUT_OFFER, verdict["findings"])
        self.assertIn(GATES.FINDING_CATALOG_ABSENT, verdict["findings"])
        no_model = _catalog_gate(completions_total=0, tools_offered=[])
        self.assertIn(GATES.FINDING_NO_MODEL_COMPLETION, no_model["findings"])
        self.assertTrue(no_model["zero"])

    def test_negation_family_needs_the_semantic_top_k(self) -> None:
        prompt = "don't search the web for this, just tell me what you think"
        top = GATES.semantic_top_k(prompt, CATALOG, GATES.SAFE_HARBOR_TOP_K)
        trimmed_out = [name for name in ALL_TOOLS if name not in top]
        fired = _catalog_gate(
            category="negation_no_tool", prompt=prompt, tools_offered=trimmed_out
        )
        self.assertIn(GATES.FINDING_RESTRAINT_WITHOUT_OFFER, fired["findings"])
        kept = _catalog_gate(
            category="negation_no_tool", prompt=prompt, tools_offered=top
        )
        self.assertEqual(kept["safe_harbor"], GATES.SAFE_HARBOR_SEMANTIC_TOP_K)
        self.assertFalse(kept["zero"])

    def test_swallowed_model_call_is_a_host_override(self) -> None:
        verdict = _catalog_gate(
            model_emitted_tool_calls=["set_theme"], observed_tool_calls=[]
        )
        self.assertIn(GATES.FINDING_SWALLOWED_MODEL_CALL, verdict["findings"])
        self.assertTrue(verdict["zero"])
        executed = _catalog_gate(
            model_emitted_tool_calls=["set_theme"], observed_tool_calls=["set_theme"]
        )
        self.assertNotIn(GATES.FINDING_SWALLOWED_MODEL_CALL, executed["findings"])
        memory = _catalog_gate(
            model_emitted_tool_calls=["search_memories"], observed_tool_calls=[]
        )
        self.assertNotIn(GATES.FINDING_SWALLOWED_MODEL_CALL, memory["findings"])

    def test_expected_tool_not_offered_unless_the_top_k_was_kept(self) -> None:
        prompt = "search the web for the latest quantum computing news"
        fired = _catalog_gate(
            category="web_search",
            prompt=prompt,
            expected_tools=["search_web"],
            tools_offered=["set_theme"],
        )
        self.assertIn(GATES.FINDING_EXPECTED_TOOL_NOT_OFFERED, fired["findings"])
        self.assertTrue(fired["zero"])
        # Memory tools are harness-internal and never required on the wire.
        routed = _catalog_gate(
            category="memory_lookup",
            prompt=prompt,
            expected_tools=["search_memories"],
            tools_offered=["set_theme"],
        )
        self.assertEqual(routed["findings"], [])
        offered = _catalog_gate(
            category="web_search", prompt=prompt, expected_tools=["search_web"]
        )
        self.assertEqual(offered["findings"], [])

    def test_missing_evidence_fails_open(self) -> None:
        verdict = _catalog_gate(completions_total=None)
        self.assertFalse(verdict["settled"])
        self.assertFalse(verdict["zero"])
        self.assertEqual(verdict["findings"], [GATES.FINDING_EVIDENCE_UNAVAILABLE])


def _artifacts() -> tuple[
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    dict[str, object],
    dict[str, object],
]:
    """A synthetic pass-off run: one honest tool case, one withheld-catalog
    tool case, an honest memory case, a /100-rewrite memory case, a
    compute-then-launder case, a concordant decision twin pair, and a
    metamorphic base/counterfactual pair."""
    dataset: dict[str, object] = {
        "seed": 41,
        "bench_version": 13,
        "tool_cases": [
            {
                "id": "t-honest",
                "category": "web_search",
                "prompt": "search the web for quantum news",
                "expected_tools": [{"name": "search_web"}],
            },
            {
                "id": "t-withheld",
                "category": "no_tool",
                "prompt": "how are you today?",
                "expected_tools": [],
            },
        ],
        "memory_waves": [
            {
                "pairs": [
                    {
                        "prompt": "I paid 411067 cents in April.",
                        "response": "Noted: 411067 cents in April.",
                    }
                ]
            }
        ],
        "memory_cases": [
            {
                "id": "m-honest",
                "question": "What did I pay in April, in minor units?",
                "expected_answer": "411067",
                "answer_kind": "money",
                "question_type": "story-money",
            },
            {
                "id": "m-rewrite",
                "question": "What did I pay in April, in minor units?",
                "expected_answer": "411067",
                "answer_kind": "money",
                "question_type": "story-money",
            },
            {
                "id": "m-launder",
                "question": "What is the remaining balance?",
                "expected_answer": "260195",
                "answer_kind": "money",
                "question_type": "program",
            },
            {
                "id": "m-twin-a",
                "question": "Which font did I ask for?",
                "expected_answer": "Inter Tight",
                "answer_kind": "clarify",
                "twin_group": "dt-1",
                "twin_relation": "decision_twin",
                "question_type": "restraint",
            },
            {
                "id": "m-twin-b",
                "question": "Which font did I ask for?",
                "expected_answer": "Inter",
                "answer_kind": "value",
                "twin_group": "dt-1",
                "twin_relation": "decision_twin",
                "question_type": "restraint",
            },
            {
                "id": "m-base",
                "question": "How much is still owed?",
                "expected_answer": "1200",
                "answer_kind": "money",
                "twin_group": "mg-1",
                "v10_provenance": {"relation": "base"},
                "question_type": "program",
            },
            {
                "id": "m-counter",
                "question": "How much is still owed after the revision?",
                "expected_answer": "3700",
                "answer_kind": "money",
                "twin_group": "mg-1",
                "v10_provenance": {"relation": "causal_counterfactual"},
                "question_type": "program",
            },
        ],
    }

    def case(
        case_id: str, kind: str, final_text: str, **extra: object
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "final_text": final_text,
            "tool_calls": [],
            "prompt_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 1,
        }
        response.update(extra)
        return {
            "case_id": case_id,
            "kind": kind,
            "response": response,
            "observed": [],
            "execution": {},
        }

    transcript: dict[str, object] = {
        "run_id": "run-1",
        "bench_version": 13,
        "cases": [
            {
                **case("t-honest", "tool", "Here is the latest quantum news."),
                "observed": [
                    {
                        "name": "search_web",
                        "args": {"queries": ["quantum news"]},
                        "hop": 0,
                    }
                ],
            },
            case("t-withheld", "tool", "I'm doing well, thanks!"),
            case(
                "m-honest",
                "memory",
                "You paid 411067 cents in April.\nAnswer: 411067",
                answer="411067",
            ),
            case(
                "m-rewrite", "memory", "You paid $4,110.67 in April.", answer="4110.67"
            ),
            case(
                "m-launder",
                "memory",
                "The remaining balance is 260195.",
                answer="260195",
            ),
            case(
                "m-twin-a",
                "memory",
                "Which typeface would you like? I found Inter and Inter Tight in your notes.",
                abstain=True,
            ),
            case(
                "m-twin-b",
                "memory",
                "Which typeface would you like? I found Inter and Inter Tight in your notes.",
                abstain=True,
            ),
            case("m-base", "memory", "You still owe 1200.", answer="1200"),
            case("m-counter", "memory", "You still owe 1200.", answer="1200"),
        ],
    }
    wire = {
        case_id: f"wire-{case_id}"
        for case_id in (
            "t-honest",
            "t-withheld",
            "m-honest",
            "m-rewrite",
            "m-launder",
            "m-twin-a",
            "m-twin-b",
            "m-base",
            "m-counter",
        )
    }
    projection: dict[str, object] = {
        "cases": [{"internal": internal, "wire": w} for internal, w in wire.items()]
    }

    def entry(
        case_id: str, completions: list[dict[str, object]], **extra: object
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "case_id": wire[case_id],
            "bench_version": 9,
            "user_input": "q",
            "catalog": CATALOG,
            "tools_offered": ALL_TOOLS,
            "harness_spans": ["You are Ditto. State the answer in the unit asked for."],
            "record_spans": [
                'Relevant memory context for this turn:\n{"longTerm":["I paid 411067 cents in April."]}'
            ],
            "tool_results": [],
            "completions": completions,
        }
        value.update(extra)
        return value

    completions: list[dict[str, object]] = [
        entry(
            "t-honest",
            [
                {
                    "text": "",
                    "tool_calls": [
                        {"name": "search_web", "args": {"queries": ["quantum news"]}}
                    ],
                },
                {"text": "Here is the latest quantum news.", "tool_calls": []},
            ],
        ),
        entry(
            "t-withheld",
            [{"text": "I'm doing well, thanks!", "tool_calls": []}],
            tools_offered=[],
        ),
        entry(
            "m-honest",
            [
                {
                    "text": "You paid 411067 cents in April.\nAnswer: 411067",
                    "tool_calls": [],
                }
            ],
        ),
        entry(
            "m-rewrite", [{"text": "You paid 411067 cents in April.", "tool_calls": []}]
        ),
        entry(
            "m-launder",
            [{"text": "The remaining balance is 260195.", "tool_calls": []}],
            harness_spans=[
                "You are Ditto. Reply exactly: The remaining balance is 260195."
            ],
            record_spans=[],
        ),
        entry(
            "m-twin-a",
            [
                {
                    "text": "Which typeface would you like? I found Inter and Inter Tight in your notes.",
                    "tool_calls": [],
                }
            ],
        ),
        entry(
            "m-twin-b",
            [
                {
                    "text": "Which typeface would you like? I found Inter and Inter Tight in your notes.",
                    "tool_calls": [],
                }
            ],
        ),
        entry("m-base", [{"text": "You still owe 1200.", "tool_calls": []}]),
        entry("m-counter", [{"text": "You still owe 1200.", "tool_calls": []}]),
    ]
    report: dict[str, object] = {
        "composite": 0.5,
        "tool_mean": 1.0,
        "memory_mean": 0.5,
        "per_case": [
            {"case_id": "t-honest", "kind": "tool", "score": 1.0, "tool_score": 1.0},
            {"case_id": "t-withheld", "kind": "tool", "score": 1.0, "tool_score": 1.0},
            {"case_id": "m-honest", "kind": "memory", "score": 1.0, "correct": True},
            {"case_id": "m-rewrite", "kind": "memory", "score": 1.0, "correct": True},
            {"case_id": "m-launder", "kind": "memory", "score": 1.0, "correct": True},
            {"case_id": "m-twin-a", "kind": "memory", "score": 1.0, "correct": True},
            {"case_id": "m-twin-b", "kind": "memory", "score": 0.0, "correct": False},
            {"case_id": "m-base", "kind": "memory", "score": 1.0, "correct": True},
            {"case_id": "m-counter", "kind": "memory", "score": 0.0, "correct": False},
        ],
    }
    return dataset, transcript, completions, projection, report


class GateReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        dataset, transcript, completions, projection, report = _artifacts()
        self.result = GATES.evaluate_run(
            dataset, transcript, completions, report, projection
        )
        self.by_case = {record["case_id"]: record for record in self.result["per_case"]}

    def test_result_is_shadow_and_salt_zero(self) -> None:
        self.assertEqual(self.result["posture"], "shadow")
        self.assertEqual(self.result["surface_salt"], 0)
        self.assertEqual(self.result["bench_version"], 13)
        self.assertEqual(len(self.result["per_case"]), 9)

    def test_honest_cases_carry_no_gate_finding(self) -> None:
        for case_id in ("t-honest", "m-honest"):
            record = self.by_case[case_id]
            self.assertFalse(record["would_zero"], record)
            self.assertFalse(set(record["findings"]) & set(GATES.GATE_FINDINGS), record)
        self.assertEqual(
            self.by_case["t-honest"]["evidence_source"], "harness_completion_log"
        )

    def test_withheld_catalog_and_rewrite_and_launder_are_noted(self) -> None:
        self.assertIn(
            GATES.FINDING_RESTRAINT_WITHOUT_OFFER,
            self.by_case["t-withheld"]["findings"],
        )
        rewrite = self.by_case["m-rewrite"]
        self.assertIn(GATES.FINDING_SERVED_TEXT_NOT_MODEL_EMITTED, rewrite["findings"])
        # The prose carries $4,110.67 beside the 4110.67 slot: not slot_not_in_prose.
        self.assertNotIn(GATES.FINDING_SLOT_NOT_IN_PROSE, rewrite["findings"])
        launder = self.by_case["m-launder"]
        self.assertIn(GATES.FINDING_ANSWER_IN_PROMPT, launder["findings"])
        self.assertNotIn(
            GATES.FINDING_SERVED_TEXT_NOT_MODEL_EMITTED, launder["findings"]
        )

    def test_twin_post_pass_notes_concordant_and_insensitive_pairs(self) -> None:
        for case_id in ("m-twin-a", "m-twin-b"):
            self.assertIn(
                GATES.FINDING_TWIN_CONCORDANT, self.by_case[case_id]["findings"]
            )
            self.assertTrue(
                any(
                    "R2 pair-product 0" in note
                    for note in self.by_case[case_id]["notes"]
                )
            )
        for case_id in ("m-base", "m-counter"):
            self.assertIn(
                GATES.FINDING_COUNTERFACTUAL_INSENSITIVE,
                self.by_case[case_id]["findings"],
            )

    def test_summary_counts_and_shadow_loss(self) -> None:
        summary = self.result["summary"]
        self.assertEqual(summary["tool_cases"], 2)
        self.assertEqual(summary["memory_cases"], 7)
        self.assertEqual(summary["findings"][GATES.FINDING_RESTRAINT_WITHOUT_OFFER], 1)
        self.assertEqual(
            summary["findings"][GATES.FINDING_SERVED_TEXT_NOT_MODEL_EMITTED], 1
        )
        self.assertEqual(summary["findings"][GATES.FINDING_ANSWER_IN_PROMPT], 1)
        self.assertEqual(summary["findings"][GATES.FINDING_TWIN_CONCORDANT], 2)
        self.assertEqual(
            summary["findings"][GATES.FINDING_COUNTERFACTUAL_INSENSITIVE], 2
        )
        loss = summary["gate_induced_loss_shadow"]
        # tool mean 1.0 -> 0.5 (t-withheld zeroed); memory 5/7 -> 1/7 (only m-honest survives).
        self.assertAlmostEqual(
            loss["composite_before_gates"], 0.5 * 1.0 + 0.5 * 5 / 7, places=6
        )
        self.assertAlmostEqual(
            loss["composite_if_enforced"], 0.5 * 0.5 + 0.5 * 1 / 7, places=6
        )
        self.assertGreater(loss["loss"], 0)

    def test_format_lists_findings_and_shadow_disclaimer(self) -> None:
        text = GATES.format_gates(self.result)
        self.assertIn("shadow replay, surface salt 0", text)
        self.assertIn("would-zero cases:", text)
        self.assertIn("m-rewrite", text)
        self.assertIn("served_text_not_model_emitted", text)
        self.assertIn("nothing above moved this score", text)

    def test_validator_relay_record_outranks_the_harness_log(self) -> None:
        dataset, transcript, completions, projection, report = _artifacts()
        cases = transcript["cases"]
        assert isinstance(cases, list)
        cases[1]["execution"] = {
            "catalog": {
                "completions_total": 1,
                "complete": True,
                "catalog_present": True,
                "tools_offered": [{"name": name} for name in ALL_TOOLS],
                "model_emitted_tool_calls": [],
            }
        }
        result = GATES.evaluate_run(
            dataset, transcript, completions, report, projection
        )
        record = next(r for r in result["per_case"] if r["case_id"] == "t-withheld")
        self.assertEqual(record["evidence_source"], "validator_relay")
        self.assertNotIn(GATES.FINDING_RESTRAINT_WITHOUT_OFFER, record["findings"])

    def test_missing_log_is_reported_not_charged(self) -> None:
        dataset, transcript, _, projection, report = _artifacts()
        result = GATES.evaluate_run(dataset, transcript, [], report, projection)
        by_case = {r["case_id"]: r for r in result["per_case"]}
        self.assertIn(
            GATES.FINDING_EVIDENCE_UNAVAILABLE, by_case["t-withheld"]["findings"]
        )
        self.assertFalse(by_case["t-withheld"]["would_zero"])
        self.assertIn(
            GATES.FINDING_PROVENANCE_UNAVAILABLE, by_case["m-honest"]["findings"]
        )
        self.assertFalse(by_case["m-honest"]["would_zero"])

    def test_offline_cli_replays_kept_artifacts(self) -> None:
        dataset, transcript, completions, projection, report = _artifacts()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dataset.json").write_text(json.dumps(dataset))
            (root / "transcript.json").write_text(json.dumps(transcript))
            (root / "projection.json").write_text(json.dumps(projection))
            (root / "report.json").write_text(
                json.dumps({"run_id": "run-1", "report": report})
            )
            (root / "completions.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in completions)
            )
            out = root / "gates.json"
            status = GATES.main(
                [
                    "--dataset",
                    str(root / "dataset.json"),
                    "--transcript",
                    str(root / "transcript.json"),
                    "--completions",
                    str(root / "completions.jsonl"),
                    "--projection",
                    str(root / "projection.json"),
                    "--report",
                    str(root / "report.json"),
                    "--out",
                    str(out),
                ]
            )
            self.assertEqual(status, 0)
            written = json.loads(out.read_text())
            self.assertEqual(
                written["summary"]["would_zero_cases"],
                self.result["summary"]["would_zero_cases"],
            )

    def test_replay_and_keep_artifacts_from_a_rehearsal_directory(self) -> None:
        dataset, transcript, completions, projection, report = _artifacts()
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            (tmp / "artifacts").mkdir()
            (tmp / "private-projections").mkdir()
            (tmp / "artifacts" / "run-1.json").write_text(json.dumps(dataset))
            (tmp / "artifacts" / "run-1.transcript.json").write_text(
                json.dumps(transcript)
            )
            (tmp / "private-projections" / "run-1.projection.json").write_text(
                json.dumps(projection)
            )
            (tmp / "completions.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in completions)
            )
            completed = {"run_id": "run-1", "status": "done", "report": report}
            gates = LOCAL.replay_gates(tmp, completed)
            self.assertEqual(
                gates["summary"]["would_zero_cases"],
                self.result["summary"]["would_zero_cases"],
            )
            kept = LOCAL.keep_gate_artifacts(tmp / "kept", tmp, completed, gates)
            names = sorted(path.name for path in kept)
            self.assertEqual(
                names,
                [
                    "completions.jsonl",
                    "dataset.json",
                    "gates.json",
                    "projection.json",
                    "report.json",
                    "transcript.json",
                ],
            )
            for path in kept:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
            with self.assertRaisesRegex(LOCAL.RehearsalError, "dataset artifact"):
                LOCAL.replay_gates(tmp, {"run_id": "missing"})


if __name__ == "__main__":
    unittest.main()
