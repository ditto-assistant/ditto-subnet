from __future__ import annotations

import ast
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
        "from demo import Counter\ndef test_x():\n    assert Counter() is (1,)\n",
        "from demo import Counter\ndef test_x():\n"
        "    while True:\n        assert Counter() == 1\n",
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


def test_plain_utf8_encoding_is_parent_data_without_extra_candidate_calls(monkeypatch):
    source = """from demo import echo
def test_encoding():
    assert echo("orbital-雪".encode()) == b"orbital-\\xe9\\x9b\\xaa"
"""
    calls, closed = [], []

    class Child:
        def __init__(self, *_):
            pass

        def rpc(self, target, operation, args=None, kwargs=None):
            assert not kwargs
            calls.append((target.path, operation, args))
            return args[0]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(driver, "Child", Child)
    symbols, tests = driver.compile_suite(source, {"demo"})
    assert driver.run_suite(symbols, tests, 10001, 10001, 1) == 1
    assert calls == [(("echo",), "call", ["orbital-雪".encode()])]
    assert closed == [True]


def test_encoding_is_bounded_and_never_dispatches_subclass_hooks():
    class Hostile(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("parent hook executed")

    class Child:
        def rpc(self, *_args, **_kwargs):
            raise AssertionError("unexpected candidate call")

    node = ast.parse("value.encode()", mode="eval").body
    with pytest.raises(driver.CandidateFailure):
        driver.evaluate(node, {"value": Hostile("x")}, Child())
    for text in ["x" * (driver.MAX_MESSAGE + 1), "雪" * driver.MAX_MESSAGE, "\ud800"]:
        with pytest.raises(driver.InvalidSuite):
            driver.evaluate(node, {"value": text}, Child())
    for expression in ["value.encode('ascii')", "value.encode(errors='ignore')"]:
        with pytest.raises(driver.InvalidSuite):
            driver.evaluate(
                ast.parse(expression, mode="eval").body, {"value": "x"}, Child()
            )


def test_remote_method_receiver_is_evaluated_only_once():
    calls = []

    class Child:
        def rpc(self, target, operation, args=None, kwargs=None):
            assert operation == "call" and not args and not kwargs
            calls.append(target.path)
            return driver.Target(None, 1, ()) if target.module else b"result"

    node = ast.parse("make().encode()", mode="eval").body
    assert (
        driver.evaluate(node, {"make": driver.Target("demo", None, ("make",))}, Child())
        == b"result"
    )
    assert calls == [("make",), ("encode",)]


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


@pytest.mark.parametrize("value", [(1,), (), (b"x", [1]), {"items": (True, None)}])
def test_tuples_keep_their_type_across_wire(value):
    assert wire.unpack(wire.pack(value)) == value
    assert wire.unpack(wire.pack((1,))) != [1]


@pytest.mark.parametrize("mode, passed", [("value", 1), ("wrong", 0), ("exception", 0)])
def test_parent_data_expressions_and_bounded_loop(monkeypatch, mode, passed):
    source = """from demo import result
def test_data():
    for item in (1, 2):
        value = result(item)
        assert value[0] == item
        assert value[1] is True and value[2] is None
        assert value == (item, True, None)
"""
    closed = []

    class Child:
        def __init__(self, *_):
            pass

        def rpc(self, _target, _operation, args, _kwargs):
            if mode == "exception":
                raise driver.CandidateException(["ValueError"])
            return (args[0], mode != "wrong", None)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(driver, "Child", Child)
    symbols, tests = driver.compile_suite(source, {"demo"})
    assert driver.run_suite(symbols, tests, 10001, 10001, 1) == passed
    assert closed == [True]


@pytest.mark.parametrize(
    "outcome, passed",
    [("raised", 1), ("returned", 0), ("transport", 0), ("wrong-type", 0)],
)
def test_expected_exception_is_not_transport_failure(monkeypatch, outcome, passed):
    source = """import pytest
from demo import operation
def test_rejects():
    with pytest.raises(ValueError):
        operation("public synthetic input")
"""
    closed = []

    class Child:
        def __init__(self, *_):
            pass

        def rpc(self, *_):
            if outcome == "raised":
                raise driver.CandidateException(["ValueError"])
            if outcome == "wrong-type":
                raise driver.CandidateException(["TypeError"])
            if outcome == "transport":
                raise driver.CandidateFailure()
            return None

        def close(self):
            closed.append(True)

    monkeypatch.setattr(driver, "Child", Child)
    symbols, tests = driver.compile_suite(source, {"demo"})
    assert driver.run_suite(symbols, tests, 10001, 10001, 1) == passed
    assert closed == [True]


@pytest.mark.parametrize(
    "body",
    [
        "with pytest.raises(Exception):\n        operation()",
        "with pytest.raises(ValueError, match='private'):\n        operation()",
        "with pytest.raises(ValueError) as error:\n        operation()",
        "with operation():\n        operation()",
        "for item in ():\n        operation()\n    assert item == operation()",
        "with pytest.raises(ValueError):\n        item = operation()\n"
        "    assert item == operation()",
        "pytest = operation()\n    with pytest.raises(ValueError):\n"
        "        operation()",
        "ValueError = operation()\n    with pytest.raises(ValueError):\n"
        "        operation()",
    ],
)
def test_context_and_control_flow_admission_stays_closed(body):
    source = (
        "import pytest\nfrom demo import operation\ndef test_context():\n    "
        + body
        + "\n"
    )
    with pytest.raises(driver.InvalidSuite):
        driver.compile_suite(source, {"demo"})


def test_boolean_short_circuit_does_not_invoke_unselected_call():
    class Child:
        def rpc(self, *_):
            raise AssertionError("short circuit invoked candidate")

    names = {"operation": driver.Target("demo", None, ("operation",))}
    for source, expected in [
        ("False and operation()", False),
        ("True or operation()", True),
    ]:
        node = __import__("ast").parse(source, mode="eval").body
        assert driver.evaluate(node, names, Child()) is expected


def test_dotted_modules_and_local_pytest_are_syntax_only():
    source = """from demo_pkg.operations import operation
def test_context():
    import pytest as checks
    with checks.raises(ValueError):
        operation(-3)
"""
    symbols, tests = driver.compile_suite(source, {"demo_pkg.operations"})
    assert len(tests) == 1
    assert symbols["operation"].module == "demo_pkg.operations"
    for module in ("os.path", "pytest.helper", "demo..ops", ".demo", "dittobench_wire"):
        with pytest.raises(driver.InvalidSuite):
            driver.compile_suite(source, {module})
    before_import = (
        source.replace("    import pytest as checks\n", "")
        + "    import pytest as checks\n"
    )
    with pytest.raises(driver.InvalidSuite):
        driver.compile_suite(before_import, {"demo_pkg.operations"})


def test_local_pytest_alias_cannot_impersonate_builtin_exception():
    source = """import pytest
from demo import operation
def test_context():
    import pytest as ValueError
    with pytest.raises(ValueError):
        operation()
"""
    with pytest.raises(driver.InvalidSuite):
        driver.compile_suite(source, {"demo"})


def test_loop_resource_limit_is_a_failure_and_closes_child(monkeypatch):
    closed = []

    class Child:
        def __init__(self, *_):
            pass

        def rpc(self, *_):
            return [0] * 10001

        def close(self):
            closed.append(True)

    monkeypatch.setattr(driver, "Child", Child)
    source = """from demo import values
def test_loop():
    for value in values():
        assert value == 0
"""
    symbols, tests = driver.compile_suite(source, {"demo"})
    assert driver.run_suite(symbols, tests, 10001, 10001, 1) == 0
    assert closed == [True]
