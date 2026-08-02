import unittest

from hermes_dittobench.protocol import ProtocolError, RunRequest, SeedRequest


class ProtocolTests(unittest.TestCase):
    def test_seed_defaults_user_and_wave(self):
        request = SeedRequest.from_json(
            {
                "pairs": [
                    {
                        "pair_id": "p1",
                        "session_id": "s1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prompt": "hello",
                        "response": "world",
                    }
                ]
            }
        )
        self.assertEqual(request.user_id, "miner")
        self.assertEqual(request.wave, 0)
        self.assertEqual(request.pairs[0].pair_id, "p1")

    def test_run_keeps_validator_schema(self):
        request = RunRequest.from_json(
            {
                "case_id": "case-1",
                "system_prompt": "system",
                "user_input": "question",
                "tools": [
                    {
                        "name": "search_web",
                        "description": "search",
                        "parameters": {
                            "type": "object",
                            "properties": {"queries": {"type": "array"}},
                        },
                    }
                ],
                "tool_endpoint": "http://tool.test/tool",
                "user_id": "alice",
            }
        )
        self.assertEqual(request.tools[0].openai_schema()["function"]["parameters"]["type"], "object")
        self.assertEqual(request.user_id, "alice")

    def test_rejects_negative_wave(self):
        with self.assertRaises(ProtocolError):
            SeedRequest.from_json({"wave": -1})


if __name__ == "__main__":
    unittest.main()
