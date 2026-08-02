import argparse
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from longmemeval_adapter import (
    AdapterError,
    HarnessClient,
    NATIVE_MEMORY_TOOLS,
    entry_to_pairs,
    normalize_timestamp,
    iter_json_array,
    parse_args,
    retrieval_condition,
    run,
    sha256_file,
    stable_id,
)
from openrouter_judge_proxy import OFFICIAL_MODEL, OPENROUTER_MODEL, rewrite_request
from openrouter_reader_proxy import (
    PROFILE_REVISION as READER_PROFILE_REVISION,
    READER_MODEL,
    rewrite_request as rewrite_reader_request,
)
from summarize_results import summarize_agent


def fixture_entry():
    return {
        "question_id": "q-1",
        "question_type": "knowledge-update",
        "question": "What is my current city?",
        "answer": "Lisbon",
        "question_date": "2026/01/02 (Fri) 12:00",
        "haystack_session_ids": ["s-1", "s-2"],
        "haystack_dates": ["2025/01/01 (Wed) 10:00", "2025/02/02 (Sun) 11:00"],
        "haystack_sessions": [
            [
                {"role": "assistant", "content": "Hello", "has_answer": True},
                {"role": "user", "content": "I moved to Lisbon"},
            ],
            [
                {"role": "user", "content": "Please remember my city"},
                {"role": "user", "content": "It is still Lisbon"},
                {"role": "assistant", "content": "I will remember"},
            ],
        ],
        "answer_session_ids": ["s-1"],
    }


class PairEncodingTests(unittest.TestCase):
    def test_timestamp_is_rfc3339(self):
        self.assertEqual(normalize_timestamp("2025/02/02 (Sun) 11:00"), "2025-02-02T11:00:00Z")

    def test_irregular_roles_are_preserved_without_labels(self):
        pairs = entry_to_pairs(fixture_entry())
        self.assertEqual(
            [(p["prompt"], p["response"]) for p in pairs],
            [
                ("", "Hello"),
                ("I moved to Lisbon", ""),
                ("Please remember my city", ""),
                ("It is still Lisbon", "I will remember"),
            ],
        )
        encoded = json.dumps(pairs)
        self.assertNotIn("has_answer", encoded)
        self.assertNotIn("question_type", encoded)
        self.assertNotIn("answer_session_ids", encoded)
        self.assertTrue(all(pair["timestamp"].endswith(":00Z") for pair in pairs))

    def test_pair_ids_are_deterministic(self):
        self.assertEqual(entry_to_pairs(fixture_entry()), entry_to_pairs(fixture_entry()))

    def test_contentless_pairs_are_omitted(self):
        entry = fixture_entry()
        entry["haystack_session_ids"] = ["empty"]
        entry["haystack_dates"] = ["2025/01/01 (Wed) 10:00"]
        entry["haystack_sessions"] = [[
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
        ]]
        self.assertEqual(entry_to_pairs(entry), [])

    def test_exact_repeated_sessions_are_deduplicated(self):
        entry = fixture_entry()
        entry["haystack_session_ids"] = ["same", "same"]
        entry["haystack_dates"] = ["2025/01/01 (Wed) 10:00"] * 2
        session = [
            {"role": "user", "content": "remember this"},
            {"role": "assistant", "content": "remembered"},
        ]
        entry["haystack_sessions"] = [session, session]
        pairs = entry_to_pairs(entry)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["prompt"], "remember this")
        self.assertEqual(pairs[0]["response"], "remembered")

    def test_reused_pair_identity_keeps_last_occurrence(self):
        entry = fixture_entry()
        entry["haystack_session_ids"] = ["same", "same"]
        entry["haystack_dates"] = ["2025/01/01 (Wed) 10:00"] * 2
        entry["haystack_sessions"] = [
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "one"},
            ],
            [
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": "two"},
            ],
        ]
        pairs = entry_to_pairs(entry)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["prompt"], "second")
        self.assertEqual(pairs[0]["response"], "two")


class StreamingDatasetTests(unittest.TestCase):
    def test_json_array_streams_across_small_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            values = [{"question_id": "q-1", "text": "héllo"}, {"question_id": "q-2"}]
            path.write_text(json.dumps(values, ensure_ascii=False))
            self.assertEqual(list(iter_json_array(path, chunk_size=3)), values)

    def test_json_array_rejects_non_array_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text('{"question_id":"q-1"}')
            with self.assertRaisesRegex(AdapterError, "dataset root must be an array"):
                list(iter_json_array(path, chunk_size=2))


class RetrievalConditionTests(unittest.TestCase):
    def test_native_condition_matches_public_memory_catalog(self):
        condition, prompt, tools = retrieval_condition("native-memory-tools")
        self.assertEqual(condition, "longmemeval-s-cleaned-native-memory-tools-v2")
        self.assertIn("native memory tools", prompt)
        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "search_memories",
                "search_subjects",
                "fetch_memories",
                "search_memories_in_subjects",
            ],
        )
        self.assertEqual(tools, NATIVE_MEMORY_TOOLS)
        self.assertEqual(tools[0]["parameters"]["required"], ["queries"])
        self.assertEqual(tools[0]["parameters"]["properties"]["queries"]["type"], "array")
        self.assertEqual(tools[2]["parameters"]["required"], ["pairIds"])
        self.assertEqual(
            tools[3]["parameters"]["required"], ["subject_id", "queries"]
        )

    def test_no_tool_condition_remains_reproducible(self):
        condition, prompt, tools = retrieval_condition("no-tools")
        self.assertEqual(condition, "longmemeval-s-cleaned-native-dittobench-memory-v1")
        self.assertIn("Do not use external knowledge or tools", prompt)
        self.assertEqual(tools, [])


class JudgeProxyTests(unittest.TestCase):
    def test_only_official_model_is_rewritten(self):
        value = rewrite_request({"model": OFFICIAL_MODEL, "messages": [], "temperature": 0})
        self.assertEqual(value["model"], OPENROUTER_MODEL)
        self.assertFalse(value["stream"])

    def test_other_models_are_rejected(self):
        with self.assertRaises(ValueError):
            rewrite_request({"model": "gpt-4o-mini", "messages": []})


class ReaderProxyTests(unittest.TestCase):
    def test_reader_model_and_provider_are_pinned(self):
        value = rewrite_reader_request(
            {"model": READER_MODEL, "messages": [], "temperature": 0, "max_tokens": 100}
        )
        self.assertEqual(value["model"], READER_MODEL)
        self.assertFalse(value["stream"])
        self.assertEqual(value["provider"]["only"], ["openai"])
        self.assertFalse(value["provider"]["allow_fallbacks"])
        self.assertEqual(value["provider"]["data_collection"], "deny")
        self.assertNotIn("zdr", value["provider"])
        self.assertEqual(READER_PROFILE_REVISION, "longmemeval-openrouter-gpt41-reader-v1")

    def test_other_reader_models_are_rejected(self):
        with self.assertRaises(ValueError):
            rewrite_reader_request({"model": "qwen/qwen3-32b", "messages": []})


class SummaryTests(unittest.TestCase):
    def test_exact_cardinality_and_categories(self):
        references = {
            "q1": {"question_type": "single-session-user"},
            "q2_abs": {"question_type": "multi-session"},
        }
        hypotheses = [
            {"question_id": "q1", "hypothesis": "yes"},
            {"question_id": "q2_abs", "hypothesis": ""},
        ]
        evaluations = [
            {"question_id": "q1", "autoeval_label": {"model": OFFICIAL_MODEL, "label": True}},
            {"question_id": "q2_abs", "autoeval_label": {"model": OFFICIAL_MODEL, "label": False}},
        ]
        result = summarize_agent(hypotheses, evaluations, references)
        self.assertEqual(result["accuracy"], 0.5)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(result["empty_answers"], 1)
        self.assertEqual(result["empty_answers_judged_correct"], 0)
        self.assertEqual(result["abstention"], {"n": 1, "accuracy": 0.0})


class RecordingHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).requests.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if self.path == "/run":
            self.wfile.write(b'{"final_text":"Lisbon","tool_calls":[]}')
        else:
            self.wfile.write(b'{"ok":true}')


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        RecordingHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_runner_never_sends_reference_answer_or_type(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "data.json"
            output = root / "hypotheses.jsonl"
            dataset.write_text(json.dumps([fixture_entry()]))
            args = argparse.Namespace(
                dataset=str(dataset),
                dataset_sha256=sha256_file(dataset),
                harness_url=f"http://127.0.0.1:{self.server.server_port}",
                output=str(output),
                manifest=None,
                agent_label="test-agent",
                bench_version=8,
                answer_model="anthropic/claude-sonnet-4",
                answer_model_profile="longmemeval-v8-claude-sonnet-4-v1",
                offset=0,
                limit=None,
                seed_wave_pairs=2,
                timeout=5.0,
                attempts=1,
                resume=False,
                rebalance_pending=False,
            )
            run(args)
            sent = json.dumps(RecordingHandler.requests)
            self.assertNotIn('"answer"', sent)
            self.assertNotIn("knowledge-update", sent)
            self.assertNotIn("has_answer", sent)
            run_body = next(body for path, body in RecordingHandler.requests if path == "/run")
            self.assertEqual(
                [tool["name"] for tool in run_body["tools"]],
                [
                    "search_memories",
                    "search_subjects",
                    "fetch_memories",
                    "search_memories_in_subjects",
                ],
            )
            self.assertEqual(json.loads(output.read_text()), {"question_id": "q-1", "hypothesis": "Lisbon"})
            manifest = json.loads(Path(str(output) + ".manifest.json").read_text())
            self.assertEqual(manifest["adapter"]["bench_version"], 8)
            self.assertEqual(manifest["answer_model"], "anthropic/claude-sonnet-4")
            self.assertEqual(
                manifest["answer_model_profile"],
                "longmemeval-v8-claude-sonnet-4-v1",
            )

    def test_cli_defaults_to_v8_and_accepts_an_audited_reader_model(self):
        args = parse_args(
            [
                "--dataset",
                "dataset.json",
                "--dataset-sha256",
                "0" * 64,
                "--harness-url",
                "http://127.0.0.1:8080",
                "--output",
                "results.jsonl",
                "--agent-label",
                "agent",
                "--answer-model",
                "google/gemini-2.5-pro",
                "--answer-model-profile",
                "longmemeval-v8-gemini-2-5-pro-v1",
            ]
        )
        self.assertEqual(args.bench_version, 8)
        self.assertEqual(args.answer_model, "google/gemini-2.5-pro")

    def test_client_requires_final_text(self):
        client = HarnessClient(f"http://127.0.0.1:{self.server.server_port}", attempts=1)
        self.assertEqual(client.request("GET", "/health"), {"status": "ok"})

    def test_resume_namespace_uses_fresh_isolated_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "data.json"
            output = root / "hypotheses.jsonl"
            dataset.write_text(json.dumps([fixture_entry()]))
            args = argparse.Namespace(
                dataset=str(dataset),
                dataset_sha256=sha256_file(dataset),
                harness_url=f"http://127.0.0.1:{self.server.server_port}",
                output=str(output),
                manifest=None,
                agent_label="test-agent",
                user_id_namespace="resume-1",
                offset=0,
                limit=None,
                seed_wave_pairs=2,
                timeout=5.0,
                attempts=1,
                resume=False,
                rebalance_pending=False,
            )
            run(args)
            user_ids = {
                body["user_id"]
                for path, body in RecordingHandler.requests
                if path in {"/seed", "/run"}
            }
            self.assertEqual(len(user_ids), 1)
            self.assertEqual(next(iter(user_ids)), "lme-" + stable_id("resume-1", "q-1", size=24))
            self.assertNotEqual(next(iter(user_ids)), "lme-" + stable_id("q-1", size=24))
            seeded_pair_ids = {
                pair["pair_id"]
                for path, body in RecordingHandler.requests
                if path == "/seed"
                for pair in body["pairs"]
            }
            default_pair_ids = {pair["pair_id"] for pair in entry_to_pairs(fixture_entry())}
            self.assertTrue(seeded_pair_ids.isdisjoint(default_pair_ids))


if __name__ == "__main__":
    unittest.main()
