from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "services/dittobench-api/coding_runtime/python/driver.py"
)
wire_spec = importlib.util.spec_from_file_location(
    "dittobench_wire", SOURCE.with_name("wire.py")
)
assert wire_spec is not None and wire_spec.loader is not None
wire = importlib.util.module_from_spec(wire_spec)
sys.modules[wire_spec.name] = wire
wire_spec.loader.exec_module(wire)
spec = importlib.util.spec_from_file_location("coding_python_oracle", SOURCE)
assert spec is not None and spec.loader is not None
driver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver
spec.loader.exec_module(driver)

SUITE = """from demo import Counter
def test_add() -> None:
    counter = Counter(3)
    assert counter.add(2) == 5
def test_fresh():
    counter = Counter(3)
    assert counter.add(0) == 3
"""


@pytest.mark.parametrize(
    "body",
    [
        "import os\n",
        "from demo import *\n",
        "from demo import Counter\ndef test_x():\n    assert True\n",
        "from demo import Counter\ndef test_x():\n    assert Counter() == (1,)\n",
        "from demo import Counter\ndef test_x():\n"
        "    for x in [1]:\n        assert Counter() == x\n",
        "from demo import Counter\ndef test_x():\n    assert Counter().__class__\n",
        "from demo import Counter\ndef test_x():\n    return Counter()\n",
        "from demo import Counter\ndef test_x(x):\n    assert Counter() == x\n",
        "from demo import Counter\ndef test_x() -> Counter():\n"
        "    assert Counter() == 1\n",
        "from demo import Counter\ndef test_x():\n    assert Counter(**{})\n",
        "from demo import Counter\ndef test_x():\n    assert True, Counter()\n",
        "from demo import Counter\ndef test_x():\n"
        "    x = Counter()\n    Counter = 1\n    assert x == 1\n",
        "from demo import Counter\ndef test_x():\n"
        "    assert Counter(value=1, value=2) == 2\n",
        "from demo import Counter\ndef test_x():\n    assert Counter() == {1: 2}\n",
    ],
)
def test_rejects_unsupported_or_non_observing_suites(body):
    with pytest.raises(driver.InvalidSuite):
        driver.compile_suite(body, {"demo"})


def test_oracles_and_counts_are_parent_owned(monkeypatch):
    children = []

    class Child:
        def __init__(self, *_args):
            self.value = 0
            self.closed = False
            children.append(self)

        def rpc(self, target, operation, args=None, kwargs=None):
            assert not kwargs
            assert operation == "call"
            if target.module:
                self.value = args[0]
                return driver.Target(None, 1, ())
            self.value += args[0]
            return self.value

        def close(self):
            self.closed = True

    monkeypatch.setattr(driver, "Child", Child)
    symbols, tests = driver.compile_suite(SUITE, {"demo"})
    assert driver.run_suite(symbols, tests, 10001, 10001, 1) == 2
    assert len(children) == 2 and all(c.closed for c in children)
    symbols, tests = driver.compile_suite(SUITE.replace("== 5", "== 999"), {"demo"})
    assert driver.run_suite(symbols, tests, 10001, 10001, 1) == 1


@pytest.mark.parametrize(
    "value",
    [
        (1,),
        {"x": (1,)},
        float("inf"),
        float("nan"),
        {1: "x"},
        driver.Target(None, 1, ()),
    ],
)
def test_json_boundary_does_not_change_tuple_or_nonfinite_semantics(value):
    assert not wire.plain(value)


@pytest.mark.parametrize(
    "value",
    [b"\x00\xff", [b"a", b"b"], {"x": [b"c"]}, {"kind": "bytes", "value": "AA=="}],
)
def test_bytes_are_lossless_and_user_dicts_are_not_wire_tags(value):
    assert wire.unpack(wire.pack(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "bytes", "value": "bad"},
        {"kind": "reference", "value": 1},
        {"kind": "json", "value": float("inf")},
    ],
)
def test_bad_data_envelopes_are_rejected(value):
    with pytest.raises(ValueError):
        wire.unpack(value)


def test_rejects_duplicate_response_fields():
    with pytest.raises(driver.CandidateFailure):
        driver.unique([("id", 1), ("id", 1)])


@pytest.mark.parametrize("modules", [set(), {"os"}, {"pytest"}, {"not_demo"}])
def test_candidate_modules_are_independently_allowlisted(modules):
    with pytest.raises(driver.InvalidSuite):
        driver.compile_suite(SUITE, modules)


def test_suite_reader_rejects_links_and_escape(tmp_path):
    path = tmp_path / "suite.py"
    path.write_text(SUITE)
    assert driver.read_suite(tmp_path, "suite.py") == SUITE
    (tmp_path / "link.py").symlink_to(path)
    for relative in ("link.py", "../suite.py", str(path)):
        with pytest.raises(driver.InvalidSuite):
            driver.read_suite(tmp_path, relative)
