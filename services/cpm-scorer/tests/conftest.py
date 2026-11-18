from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPORTER = REPO_ROOT / "docs" / "research" / "cpm" / "export_cpm.py"
DATAGEN = REPO_ROOT / "research" / "dittobench-datagen"


@pytest.fixture
def tiny_artifact() -> dict:
    """A small artifact in the `cmd/generate` shape, covering waves, users,
    tool prerequisites, abstain cases, list answers and distractors."""
    return {
        "seed": 5,
        "bench_version": 18,
        "memory_waves": [
            {
                "user_id": "miner",
                "pairs": [
                    {
                        "pair_id": "p2",
                        "session_id": "s1",
                        "timestamp": "2025-01-01T00:00:00Z",
                        "prompt": "My dentist is Dr. Alvarez; billing is Acme Corp.",
                        "response": "Noted.",
                    },
                    {
                        "pair_id": "p1",
                        "session_id": "s1",
                        "timestamp": "2025-01-02T00:00:00Z",
                        "prompt": "Actually the dentist moved; not Dr. Brown.",
                        "response": "Got it, Dr. Brown is out.",
                    },
                ],
            },
            {"user_id": "miner", "wave": 1, "pairs": [{"pair_id": "p3"}]},
            {"user_id": "other", "pairs": [{"pair_id": "q1", "prompt": "hi"}]},
            {"user_id": "miner", "wave": 2},
        ],
        "memory_cases": [
            {
                "id": "m2",
                "question": "Who is my dentist?",
                "question_type": "single_hop",
                "answer_kind": "value",
                "expected_answer": "Dr. Alvarez",
                "accept_any": ["Alvarez"],
                "distractor_answers": ["Dr. Brown"],
                "v10_evidence_pair_ids": ["p2"],
                "run_after_wave": 0,
            },
            {
                "id": "m1",
                "question": "List my vendors",
                "answer_kind": "list",
                "expected_answer": "Acme Corp; Dr. Alvarez",
                "forbidden_answer": "Globex",
                "v10_evidence_pair_ids": ["p2", "missing"],
                "run_after_wave": 1,
            },
            {
                "id": "m3",
                "question": "What is my cat called?",
                "answer_kind": "absence",
                "v10_evidence_pair_ids": [],
            },
            {
                "id": "m4",
                "user_id": "other",
                "question": "hello?",
                "answer_kind": "chitchat",
                "v10_evidence_pair_ids": ["q1"],
            },
        ],
        "tool_cases": [
            {
                "id": "t1",
                "prompt": "Send the poll",
                "category": "messaging",
                "expected_tools": [{"name": "send_poll"}, {"name": "get_group"}],
                "prerequisite_pairs": [{"pair_id": "tp1", "prompt": "group is G"}],
            },
            {"id": "t0", "prompt": "Just chat", "expected_tools": []},
        ],
        "tool_fixtures": [{"case_id": "t1", "needle": "G"}],
        "catalog": [
            {"name": "send_poll", "description": "Send a poll."},
            {"name": "get_group", "description": "Read a group."},
            {"name": "delete_all", "description": "Dangerous."},
        ],
    }
