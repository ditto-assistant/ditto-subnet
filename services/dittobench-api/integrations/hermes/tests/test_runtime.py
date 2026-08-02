import unittest
from unittest.mock import patch

from hermes_dittobench.protocol import RunRequest
from hermes_dittobench.runtime import HermesRunner


class FakeState:
    def __init__(self):
        self.db = object()

    def lock_for(self, user_id):
        raise AssertionError("case execution must not take the seed/write lock")

    def db_for(self, user_id):
        return self.db


class FakeRegistry:
    def __init__(self):
        self.entries = {}

    def register(self, name, toolset, schema, handler, **kwargs):
        self.entries[name] = handler


class FakeAgent:
    registry = None
    last_instance = None

    def __init__(self, **kwargs):
        type(self).last_instance = self
        self.kwargs = kwargs
        self.callback = kwargs["tool_start_callback"]
        self.tools = []
        self.valid_tool_names = set()

    def run_conversation(self, user_input):
        tool = self.tools[0]["function"]
        args = {"queries": ["needle"]}
        self.callback("call-1", tool["name"], args)
        result = self.registry.entries[tool["name"]](args)
        return {
            "final_response": result,
            "input_tokens": 12,
            "output_tokens": 4,
        }

    def shutdown_memory_provider(self):
        return None

    def release_clients(self):
        return None


class NativeFakeAgent(FakeAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "session_search",
                    "description": "Hermes native session search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        self.valid_tool_names = {"session_search"}

    def run_conversation(self, user_input):
        self.callback("call-native-1", "session_search", {"query": "needle"})
        return {
            "final_response": "native remembered",
            "input_tokens": 13,
            "output_tokens": 3,
        }


class RuntimeTests(unittest.TestCase):
    def test_preflight_executes_observed_search_without_model(self):
        calls = []
        runner = HermesRunner(FakeState(), post_json=lambda url, body, timeout: calls.append((url, body)) or {"result": "ok"})
        response = runner.run(
            RunRequest.from_json(
                {
                    "case_id": "preflight:abc",
                    "user_input": "probe",
                    "tools": [],
                    "tool_endpoint": "http://validator/tool",
                }
            )
        )
        self.assertEqual(calls[0][1]["name"], "search_web")
        self.assertEqual(response["tool_calls"][0]["hop"], 0)

    def test_catalog_tool_is_forwarded_and_reported(self):
        calls = []
        registry = FakeRegistry()
        FakeAgent.registry = registry
        runner = HermesRunner(
            FakeState(),
            agent_factory=FakeAgent,
            registry=registry,
            session_search=lambda **kwargs: "memory",
            post_json=lambda url, body, timeout: calls.append(body) or {"result": "tool result"},
        )
        response = runner.run(
            RunRequest.from_json(
                {
                    "case_id": "tool-1",
                    "system_prompt": "system",
                    "user_input": "search",
                    "tools": [
                        {
                            "name": "search_web",
                            "description": "search",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                    "tool_endpoint": "http://validator/tool",
                }
            )
        )
        self.assertEqual(calls[0]["case_id"], "tool-1")
        self.assertEqual(calls[0]["hop"], 0)
        self.assertEqual(response["final_text"], "tool result")
        self.assertEqual(response["tool_calls"][0]["name"], "search_web")
        self.assertEqual(response["prompt_tokens"], 12)
        self.assertTrue(FakeAgent.last_instance._disable_streaming)

    def test_memory_catalog_alias_uses_native_session_search(self):
        searches = []
        registry = FakeRegistry()
        FakeAgent.registry = registry
        runner = HermesRunner(
            FakeState(),
            agent_factory=FakeAgent,
            registry=registry,
            session_search=lambda **kwargs: searches.append(kwargs) or "remembered",
        )
        response = runner.run(
            RunRequest.from_json(
                {
                    "case_id": "memory-1",
                    "user_input": "what was the needle?",
                    "tools": [
                        {
                            "name": "search_memories",
                            "description": "memory search",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                }
            )
        )
        self.assertEqual(response["final_text"], "remembered")
        self.assertEqual(searches[0]["query"], "needle")
        self.assertEqual(searches[0]["limit"], 5)
        self.assertIs(searches[0]["db"], runner.state.db)

    def test_favorable_profile_uses_documented_budget_and_broader_native_recall(self):
        searches = []
        registry = FakeRegistry()
        FakeAgent.registry = registry
        runner = HermesRunner(
            FakeState(),
            agent_factory=FakeAgent,
            registry=registry,
            session_search=lambda **kwargs: searches.append(kwargs) or "remembered",
        )
        with patch.dict("os.environ", {"HERMES_DITTOBENCH_PROFILE": "favorable"}, clear=False):
            response = runner.run(
                RunRequest.from_json(
                    {
                        "case_id": "memory-favorable",
                        "system_prompt": "benchmark system",
                        "user_input": "what was the needle?",
                        "tools": [
                            {
                                "name": "search_memories",
                                "description": "memory search",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    }
                )
            )
        self.assertEqual(response["final_text"], "remembered")
        self.assertEqual(searches[0]["limit"], 10)
        self.assertEqual(FakeAgent.last_instance.kwargs["max_iterations"], 90)
        self.assertEqual(FakeAgent.last_instance.kwargs["max_tokens"], 8192)
        prompt = FakeAgent.last_instance.kwargs["ephemeral_system_prompt"]
        self.assertIn("benchmark system", prompt)
        self.assertIn("past conversation", prompt)

    def test_native_session_profile_preserves_hermes_tool_name_and_persistence_path(self):
        registry = FakeRegistry()
        NativeFakeAgent.registry = registry
        runner = HermesRunner(
            FakeState(),
            agent_factory=NativeFakeAgent,
            registry=registry,
            session_search=lambda **kwargs: "unused alias",
        )
        with patch.dict("os.environ", {"HERMES_DITTOBENCH_PROFILE": "native-session"}, clear=False):
            response = runner.run(
                RunRequest.from_json(
                    {
                        "case_id": "memory-native",
                        "system_prompt": "benchmark system",
                        "user_input": "what was the needle?",
                        "tools": [
                            {
                                "name": "search_memories",
                                "description": "Ditto memory search",
                                "parameters": {"type": "object", "properties": {}},
                            },
                            {
                                "name": "search_web",
                                "description": "web search",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        ],
                    }
                )
            )

        self.assertEqual(response["final_text"], "native remembered")
        self.assertEqual(response["tool_calls"], [
            {"name": "session_search", "args": {"query": "needle"}, "hop": 0}
        ])
        kwargs = NativeFakeAgent.last_instance.kwargs
        self.assertEqual(kwargs["enabled_toolsets"], ["dittobench-wire", "session_search"])
        self.assertEqual(kwargs["max_tokens"], 8192)
        self.assertFalse(kwargs["skip_memory"])
        self.assertEqual(NativeFakeAgent.last_instance.valid_tool_names, {"session_search"})
        self.assertNotIn("search_memories", registry.entries)
        self.assertIn("search_web", registry.entries)

    def test_max_tokens_must_be_positive(self):
        runner = HermesRunner(
            FakeState(),
            agent_factory=FakeAgent,
            registry=FakeRegistry(),
            session_search=lambda **kwargs: "memory",
        )
        with patch.dict(
            "os.environ", {"HERMES_DITTOBENCH_MAX_TOKENS": "0"}, clear=False
        ):
            with self.assertRaisesRegex(ValueError, "must be positive"):
                runner.run(
                    RunRequest.from_json(
                        {"case_id": "case", "user_input": "hello", "tools": []}
                    )
                )

    def test_unknown_profile_fails_closed(self):
        runner = HermesRunner(
            FakeState(),
            agent_factory=FakeAgent,
            registry=FakeRegistry(),
            session_search=lambda **kwargs: "memory",
        )
        with patch.dict("os.environ", {"HERMES_DITTOBENCH_PROFILE": "unknown"}, clear=False):
            with self.assertRaisesRegex(ValueError, "baseline, favorable, or native-session"):
                runner.run(
                    RunRequest.from_json(
                        {"case_id": "case", "user_input": "hello", "tools": []}
                    )
                )


if __name__ == "__main__":
    unittest.main()
