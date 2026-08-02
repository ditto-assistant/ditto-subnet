import tempfile
import unittest
from pathlib import Path

from hermes_dittobench.protocol import SeedRequest
from hermes_dittobench.state import HermesStateStore


class FakeDB:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.sessions = []
        self.messages = []
        self.closed = False

    def create_session(self, session_id, source):
        self.sessions.append((session_id, source))

    def append_message(self, session_id, role, content, timestamp=None):
        self.messages.append((session_id, role, content, timestamp))

    def end_session(self, session_id, reason):
        return None

    def close(self):
        self.closed = True


def pair(pair_id, session_id, prompt, response):
    return {
        "pair_id": pair_id,
        "session_id": session_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "prompt": prompt,
        "response": response,
    }


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.created = []

        def factory(db_path):
            db = FakeDB(db_path)
            self.created.append(db)
            return db

        self.state = HermesStateStore(Path(self.tmp.name), session_db_factory=factory)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_staged_waves_are_idempotent_and_accumulate(self):
        self.state.seed(SeedRequest.from_json({"wave": 0, "pairs": [pair("p1", "s1", "one", "alpha")]}))
        first = self.created[-1]
        self.state.seed(SeedRequest.from_json({"wave": 1, "pairs": [pair("p2", "s2", "two", "beta")]}))
        latest = self.created[-1]
        self.assertTrue(first.closed)
        self.assertEqual([m[2] for m in latest.messages], ["one", "alpha", "two", "beta"])

        self.state.seed(SeedRequest.from_json({"wave": 1, "pairs": [pair("p2", "s2", "two", "beta")]}))
        retried = self.created[-1]
        self.assertEqual([m[2] for m in retried.messages], ["one", "alpha", "two", "beta"])

    def test_wave_zero_resets_only_that_user(self):
        self.state.seed(SeedRequest.from_json({"user_id": "alice", "wave": 0, "pairs": [pair("a", "s", "old", "value")]}))
        self.state.seed(SeedRequest.from_json({"user_id": "bob", "wave": 0, "pairs": [pair("b", "s", "bob", "secret")]}))
        alice_path = self.state._path("alice")
        bob_path = self.state._path("bob")
        self.assertNotEqual(alice_path, bob_path)

        self.state.seed(SeedRequest.from_json({"user_id": "alice", "wave": 0, "pairs": [pair("c", "s", "new", "value")]}))
        latest_alice = self.created[-1]
        self.assertEqual([m[2] for m in latest_alice.messages], ["new", "value"])
        self.assertEqual([m[2] for m in self.state.db_for("bob").messages], ["bob", "secret"])


if __name__ == "__main__":
    unittest.main()
