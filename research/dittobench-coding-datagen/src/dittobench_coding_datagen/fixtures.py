"""Small deterministic public practice repositories.

These fixtures exercise the coding workspace protocol. They are intentionally
simple, public, and permanently ineligible for scoring.
"""

from __future__ import annotations

from dataclasses import dataclass

from dittobench_coding_datagen.model import CorpusError


@dataclass(frozen=True)
class Fixture:
    base_files: dict[str, str]
    visible_tests: dict[str, str]
    grader_tests: dict[str, str]


def _fixture(test_body: str, grader_body: str = "") -> Fixture:
    return Fixture(
        base_files={"app.py": ""},
        visible_tests={"tests/test_visible.py": test_body},
        grader_tests={"tests/test_regression.py": grader_body or test_body},
    )


_FIXTURES: dict[str, Fixture] = {
    "ledger-reference": Fixture(
        base_files={
            "app.py": (
                "def normalize_reference(value: str) -> str:\n"
                "    return str(int(value.strip()))\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import normalize_reference\n\n"
                "class ReferenceTests(unittest.TestCase):\n"
                "    def test_surrounding_space_is_removed(self):\n"
                "        self.assertEqual(normalize_reference(' 00042 '), '00042')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import normalize_reference\n\n"
                "class ReferenceRegressionTests(unittest.TestCase):\n"
                "    def test_reference_is_not_numeric(self):\n"
                "        self.assertEqual(normalize_reference(' AB-07 '), 'AB-07')\n"
                "    def test_leading_zeroes_are_identity(self):\n"
                "        self.assertEqual(normalize_reference('0000'), '0000')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "ledger-allocation": Fixture(
        base_files={
            "app.py": (
                "def allocate_cents(total: int, parties: int) -> list[int]:\n"
                "    share = total // parties\n"
                "    return [share] * parties\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import allocate_cents\n\n"
                "class AllocationTests(unittest.TestCase):\n"
                "    def test_no_money_is_lost(self):\n"
                "        self.assertEqual(sum(allocate_cents(10, 3)), 10)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import allocate_cents\n\n"
                "class AllocationRegressionTests(unittest.TestCase):\n"
                "    def test_remainder_is_stable(self):\n"
                "        self.assertEqual(allocate_cents(10, 3), [4, 3, 3])\n"
                "    def test_even_split_is_unchanged(self):\n"
                "        self.assertEqual(allocate_cents(8, 2), [4, 4])\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "ledger-balance": Fixture(
        base_files={
            "app.py": (
                "def is_balanced(debits: list[int], credits: list[int]) -> bool:\n"
                "    return len(debits) == len(credits)\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import is_balanced\n\n"
                "class BalanceTests(unittest.TestCase):\n"
                "    def test_equal_totals(self):\n"
                "        self.assertTrue(is_balanced([3, 7], [10]))\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import is_balanced\n\n"
                "class BalanceRegressionTests(unittest.TestCase):\n"
                "    def test_equal_lengths_are_not_enough(self):\n"
                "        self.assertFalse(is_balanced([1, 2], [1, 3]))\n"
                "    def test_empty_ledger_is_balanced(self):\n"
                "        self.assertTrue(is_balanced([], []))\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "config-precedence": Fixture(
        base_files={
            "app.py": (
                "def merge_config(defaults: dict, environment: dict) -> dict:\n"
                "    result = dict(environment)\n"
                "    result.update(defaults)\n"
                "    return result\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import merge_config\n\n"
                "class MergeTests(unittest.TestCase):\n"
                "    def test_environment_wins(self):\n"
                "        self.assertEqual(merge_config({'port': 80}, "
                "{'port': 443})['port'], 443)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import merge_config\n\n"
                "class MergeRegressionTests(unittest.TestCase):\n"
                "    def test_defaults_remain(self):\n"
                "        self.assertEqual(merge_config({'host': 'local'}, "
                "{'port': 443}), {'host': 'local', 'port': 443})\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "config-boolean": Fixture(
        base_files={
            "app.py": (
                "def parse_bool(value: str) -> bool:\n"
                "    return value.strip().lower() in {'true', 'yes', '1'}\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import parse_bool\n\n"
                "class BooleanTests(unittest.TestCase):\n"
                "    def test_true_and_false(self):\n"
                "        self.assertTrue(parse_bool('true'))\n"
                "        self.assertFalse(parse_bool('false'))\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import parse_bool\n\n"
                "class BooleanRegressionTests(unittest.TestCase):\n"
                "    def test_legacy_synonym_is_rejected(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            parse_bool('yes')\n"
                "    def test_unknown_value_is_rejected(self):\n"
                "        with self.assertRaises(ValueError):\n"
                "            parse_bool('maybe')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "config-endpoint": Fixture(
        base_files={
            "app.py": (
                "def canonical_endpoint(value: str) -> str:\n"
                "    return value.rstrip('/') + '/'\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import canonical_endpoint\n\n"
                "class EndpointTests(unittest.TestCase):\n"
                "    def test_signature_form_has_no_trailing_slash(self):\n"
                "        self.assertEqual(canonical_endpoint("
                "'https://service.invalid/'), 'https://service.invalid')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import canonical_endpoint\n\n"
                "class EndpointRegressionTests(unittest.TestCase):\n"
                "    def test_already_canonical(self):\n"
                "        self.assertEqual(canonical_endpoint("
                "'https://service.invalid'), 'https://service.invalid')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "cache-key": Fixture(
        base_files={
            "app.py": (
                "def cache_key(namespace: str, item: str) -> str:\n"
                "    return f'{namespace.strip()}:{item.strip()}'\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import cache_key\n\n"
                "class CacheKeyTests(unittest.TestCase):\n"
                "    def test_namespace_is_case_insensitive(self):\n"
                "        self.assertEqual(cache_key('API', 'x'), "
                "cache_key('api', 'x'))\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import cache_key\n\n"
                "class CacheKeyRegressionTests(unittest.TestCase):\n"
                "    def test_item_identity_remains_case_sensitive(self):\n"
                "        self.assertNotEqual(cache_key('api', 'X'), "
                "cache_key('api', 'x'))\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "cache-ttl": Fixture(
        base_files={
            "app.py": "def normalize_ttl(seconds: int) -> int:\n    return seconds\n"
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import normalize_ttl\n\n"
                "class TtlTests(unittest.TestCase):\n"
                "    def test_negative_ttl_is_zero(self):\n"
                "        self.assertEqual(normalize_ttl(-5), 0)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import normalize_ttl\n\n"
                "class TtlRegressionTests(unittest.TestCase):\n"
                "    def test_positive_ttl_is_preserved(self):\n"
                "        self.assertEqual(normalize_ttl(12), 12)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
    "cache-eviction": Fixture(
        base_files={
            "app.py": (
                "def eviction_candidate(entries: list[tuple[str, int]]) -> str:\n"
                "    return max(entries, key=lambda entry: entry[1])[0]\n"
            )
        },
        visible_tests={
            "tests/test_visible.py": (
                "import unittest\n\n"
                "from app import eviction_candidate\n\n"
                "class EvictionTests(unittest.TestCase):\n"
                "    def test_oldest_entry_is_evicted(self):\n"
                "        self.assertEqual(eviction_candidate("
                "[('old', 1), ('new', 9)]), 'old')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
        grader_tests={
            "tests/test_regression.py": (
                "import unittest\n\n"
                "from app import eviction_candidate\n\n"
                "class EvictionRegressionTests(unittest.TestCase):\n"
                "    def test_order_does_not_change_result(self):\n"
                "        self.assertEqual(eviction_candidate("
                "[('new', 9), ('old', 1)]), 'old')\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            )
        },
    ),
}


def fixture_for(kind: str) -> Fixture:
    try:
        return _FIXTURES[kind]
    except KeyError as error:
        raise CorpusError(f"unknown public practice fixture kind: {kind!r}") from error


def fixture_kinds() -> frozenset[str]:
    return frozenset(_FIXTURES)
