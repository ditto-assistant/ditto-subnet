"""Contract checks for the transparent source-review control."""

import unittest
from unittest.mock import patch

import agent


class ControlContractTest(unittest.TestCase):
    def setUp(self) -> None:
        agent._records.clear()
        agent._subjects.clear()
        agent._links.clear()
        agent.seed(
            {
                "user_id": "alice",
                "pairs": [{"pair_id": "a", "prompt": "Where?", "response": "Paris"}],
            }
        )
        agent.seed(
            {
                "user_id": "bob",
                "pairs": [{"pair_id": "b", "prompt": "Where?", "response": "Rome"}],
            }
        )

    def test_user_records_and_model_text_are_preserved(self) -> None:
        def fake_post(url, payload, *, broker=False):
            self.assertTrue(url.endswith("/chat/completions"))
            self.assertTrue(broker)
            self.assertIn("Paris", payload["messages"][1]["content"])
            self.assertNotIn("Rome", payload["messages"][1]["content"])
            self.assertNotIn("tools", payload)
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Paris, per your record.",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            }

        with patch.object(agent, "_post_json", side_effect=fake_post):
            result = agent.run(
                {
                    "case_id": "case-1",
                    "user_id": "alice",
                    "system_prompt": "Use the records.",
                    "user_input": "Where?",
                    "tools": [],
                    "inference_base_url": "http://broker/run/case-1",
                }
            )
        self.assertEqual(result["final_text"], "Paris, per your record.")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(result["prompt_tokens"], 12)

    def test_model_selected_call_is_executed_and_reported(self) -> None:
        calls = []

        def fake_post(url, payload, *, broker=False):
            calls.append((url, payload))
            if url == "http://validator/tool":
                self.assertFalse(broker)
                return {"result": "dark"}
            self.assertTrue(broker)
            if len([item for item in calls if "chat/completions" in item[0]]) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "set_theme",
                                            "arguments": '{"theme":"dark"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }
            self.assertEqual(payload["messages"][-1]["content"], '{"result": "dark"}')
            return {
                "choices": [{"message": {"role": "assistant", "content": "Done."}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            }

        with patch.object(agent, "_post_json", side_effect=fake_post):
            result = agent.run(
                {
                    "case_id": "case-2",
                    "user_id": "alice",
                    "system_prompt": "Use the records.",
                    "user_input": "Set theme to dark",
                    "tools": [
                        {
                            "name": "set_theme",
                            "description": "Set the theme",
                            "parameters": {
                                "type": "object",
                                "properties": {"theme": {"type": "string"}},
                            },
                        }
                    ],
                    "tool_endpoint": "http://validator/tool",
                    "inference_base_url": "http://broker/run/case-2",
                }
            )
        self.assertEqual(result["final_text"], "Done.")
        self.assertEqual(
            result["tool_calls"],
            [{"name": "set_theme", "args": {"theme": "dark"}, "hop": 0}],
        )
        self.assertEqual(calls[1][1]["args"], {"theme": "dark"})
        self.assertEqual(result["prompt_tokens"], 22)
        self.assertEqual(result["output_tokens"], 7)

    def test_missing_gateway_usage_fails_closed(self) -> None:
        with (
            patch.object(
                agent,
                "_post_json",
                return_value={
                    "choices": [{"message": {"role": "assistant", "content": "Paris"}}]
                },
            ),
            self.assertRaisesRegex(ValueError, "omitted valid token usage"),
        ):
            agent.run(
                {
                    "case_id": "case-3",
                    "user_id": "alice",
                    "system_prompt": "Use the records.",
                    "user_input": "Where?",
                    "tools": [],
                    "inference_base_url": "http://broker/run/case-3",
                }
            )

    def test_local_memory_tool_is_scoped_to_request_user(self) -> None:
        self.assertEqual(
            agent._local_memory_tool(
                "alice", "fetch_memories", {"pairIds": ["a", "b"]}
            ),
            {"memories": [{"pair_id": "a", "prompt": "Where?", "response": "Paris"}]},
        )
        self.assertEqual(
            agent._local_memory_tool("bob", "search_memories", {"queries": ["Paris"]}),
            {"memories": []},
        )


if __name__ == "__main__":
    unittest.main()
