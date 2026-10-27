"""Offline checks on the forked LongMemEval judge evaluator.

The fork's whole claim is "we changed the judge and the completion bound and
nothing else". That is checked here against the two recorded evaluator files in
``docs/longmemeval-benchmark/judge-evaluator/`` rather than asserted in prose,
including a behavioural run of both against a stubbed client: upstream silently
labels a truncated verdict "no", and the fork refuses it.
"""

from __future__ import annotations

import ast
import contextlib
import re
import shutil
import hashlib
import io
import json
import runpy
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from typing import Any, NamedTuple

import judge_evaluator

REPO_ROOT = Path(__file__).resolve().parents[4]
EVALUATOR_DIR = REPO_ROOT / "docs" / "longmemeval-benchmark" / "judge-evaluator"
UPSTREAM_EVALUATOR = EVALUATOR_DIR / "upstream" / "evaluate_qa.py"
DITTO_EVALUATOR = EVALUATOR_DIR / "ditto" / "evaluate_qa.py"
DELTA_PATCH = EVALUATOR_DIR / "ditto-judge-v1.patch"
PROVIDER_RUNTIME = (
    REPO_ROOT
    / "services"
    / "dittobench-api"
    / "internal"
    / "longmemeval"
    / "provider_runtime.go"
)
_GO_CONSTANT = re.compile(r"^\s*(officialJudge\w+)\s*=\s*(.+?)\s*$", re.MULTILINE)
# The block lives in judgeProviderRouting's returned literal. Anchoring on the
# function keeps the parser pointed at the real source rather than at any
# `map[string]any{` that happens to be nearby.
_GO_PROVIDER_BLOCK = re.compile(
    r"func judgeProviderRouting\([^)]*\)\s*map\[string\]any\s*\{\s*"
    r"return map\[string\]any\{(.*?)\n\t*\}",
    re.DOTALL,
)
_GO_PROVIDER_ENTRY = re.compile(r'"(\w+)":\s*(.+?),\s*$', re.MULTILINE)


def go_provider_block(route_provider: str) -> dict[str, object]:
    """The judge lane's OpenRouter provider block, read out of the Go runtime.

    The relay compares this block as a unit, so the Python helper has to
    reproduce all of it rather than the pin alone.
    """
    blocks = _GO_PROVIDER_BLOCK.findall(PROVIDER_RUNTIME.read_text())
    if len(blocks) != 1:
        raise AssertionError(
            f"expected exactly 1 provider block in {PROVIDER_RUNTIME.name}, "
            f"found {len(blocks)}"
        )
    parsed: dict[str, object] = {}
    for key, literal in _GO_PROVIDER_ENTRY.findall(blocks[0]):
        literal = literal.strip()
        if literal == "false":
            parsed[key] = False
        elif literal == "true":
            parsed[key] = True
        elif literal.startswith('"') and literal.endswith('"'):
            parsed[key] = literal[1:-1]
        elif literal.startswith("[]string{"):
            # Mirroring any []string{...} as a single-element pin would let the
            # parity assertion pass while the Go slice had drifted -- a second
            # element, or a different expression, would be invisible. Only the
            # exact single-element pin this helper can faithfully mirror is
            # accepted; anything else is a parse failure, not a match.
            inner = literal[len("[]string{") : -1].strip()
            if not literal.endswith("}") or inner != "routeProvider":
                raise AssertionError(
                    f"unexpected Go provider slice for {key}: {literal!r}; "
                    "this helper only mirrors a single-element routeProvider pin"
                )
            parsed[key] = [route_provider]
        else:
            raise AssertionError(f"unparsed Go provider literal: {key}={literal!r}")
    return parsed


def go_judge_constants() -> dict[str, object]:
    """The production judge lane's constants, read out of the Go runtime."""
    values: dict[str, object] = {}
    for name, literal in _GO_CONSTANT.findall(PROVIDER_RUNTIME.read_text()):
        if literal.startswith('"') and literal.endswith('"'):
            values[name] = literal[1:-1]
        elif literal.isdigit():
            values[name] = int(literal)
    return values


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompt_function(path: Path) -> tuple[str, list[str]]:
    """The judge prompt builder's source and its five templates."""
    text = path.read_text(encoding="utf-8")
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == "get_anscheck_prompt":
            segment = ast.get_source_segment(text, node)
            assert segment is not None
            templates = sorted(
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "Model Response" in child.value
            )
            return segment, templates
    raise AssertionError(f"{path}: get_anscheck_prompt not found")


def module_constant(path: Path, name: str) -> object:
    """Evaluate one module-level literal assignment without importing."""
    text = path.read_text(encoding="utf-8")
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path}: {name} not found")


class StubCompletion:
    def __init__(self, content: str | None, finish_reason: str) -> None:
        message = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
        self.choices = [choice]


class StubClient:
    """Records judge calls and replays one canned completion."""

    def __init__(self, content: str | None, finish_reason: str) -> None:
        self.calls: list[dict[str, object]] = []
        create = self._create
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)
        )
        self._content = content
        self._finish_reason = finish_reason

    def _create(self, **kwargs: object) -> StubCompletion:
        self.calls.append(kwargs)
        return StubCompletion(self._content, self._finish_reason)


class EvaluatorRun(NamedTuple):
    client: StubClient
    result_name: str
    rows: list[dict[str, Any]]


def run_evaluator(
    evaluator: Path,
    judge_short_name: str,
    *,
    content: str | None,
    finish_reason: str = "stop",
) -> EvaluatorRun:
    """Execute one evaluator against a stubbed OpenAI client.

    Returns the recorded calls, the result file's name and its parsed rows.
    The scratch directory is removed on every path, including when the
    evaluator raises, so the result is read here rather than handed back as a
    path into a directory that no longer exists.
    """
    client = StubClient(content, finish_reason)

    openai_stub = types.ModuleType("openai")
    openai_stub.RateLimitError = type("RateLimitError", (Exception,), {})
    openai_stub.APIError = type("APIError", (Exception,), {})
    openai_stub.OpenAI = lambda **_kwargs: client
    openai_stub.organization = None

    backoff_stub = types.ModuleType("backoff")
    backoff_stub.expo = object()
    backoff_stub.on_exception = lambda *_a, **_k: (lambda fn: fn)

    tqdm_stub = types.ModuleType("tqdm")
    tqdm_stub.tqdm = lambda iterable, **_kwargs: iterable

    # The evaluator calls both `np.mean(...).item()` and `round(np.mean(...), 4)`,
    # so the stub has to be a real number as well as expose `.item()`.
    class Mean(float):
        def item(self) -> float:
            return float(self)

    numpy_stub = types.ModuleType("numpy")
    numpy_stub.mean = lambda values: Mean(
        (sum(values) / len(values)) if len(values) else 0.0
    )

    reference = [
        {
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "Which city did I move to?",
            "answer": "Lisbon",
        }
    ]
    hypotheses = [{"question_id": "q1", "hypothesis": "You moved to Lisbon."}]

    directory = Path(tempfile.mkdtemp(prefix="longmemeval-judge-test-"))
    hypothesis_path = directory / "hyp.jsonl"
    reference_path = directory / "ref.json"
    hypothesis_path.write_text(
        "\n".join(json.dumps(row) for row in hypotheses), encoding="utf-8"
    )
    reference_path.write_text(json.dumps(reference), encoding="utf-8")

    saved_modules = {
        name: sys.modules.get(name)
        for name in ("openai", "backoff", "tqdm", "numpy")
    }
    saved_argv = sys.argv
    sys.modules.update(
        openai=openai_stub, backoff=backoff_stub, tqdm=tqdm_stub, numpy=numpy_stub
    )
    sys.argv = [
        str(evaluator),
        judge_short_name,
        str(hypothesis_path),
        str(reference_path),
    ]
    suffix = judge_evaluator.evaluation_suffix(judge_short_name)
    result_path = hypothesis_path.with_name(hypothesis_path.name + suffix)
    try:
        # The evaluator prints every verdict and a final accuracy table; keep
        # that out of the test report without hiding exceptions.
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            # Both evaluators read their inputs with a bare `open()`. That is
            # upstream's code and the fork deliberately does not touch it, so
            # the ResourceWarning is expected rather than a defect to report.
            warnings.simplefilter("ignore", ResourceWarning)
            runpy.run_path(str(evaluator), run_name="__main__")
        rows = [
            json.loads(line)
            for line in result_path.read_text().splitlines()
            if line.strip()
        ]
        return EvaluatorRun(client, result_path.name, rows)
    finally:
        sys.argv = saved_argv
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        shutil.rmtree(directory, ignore_errors=True)


class RecordedEvaluatorTests(unittest.TestCase):
    def test_recorded_files_exist(self):
        for path in (UPSTREAM_EVALUATOR, DITTO_EVALUATOR, DELTA_PATCH):
            self.assertTrue(path.is_file(), f"missing recorded artifact: {path}")

    def test_recorded_digests_match_the_declared_identity(self):
        self.assertEqual(
            sha256(UPSTREAM_EVALUATOR), judge_evaluator.UPSTREAM_EVALUATOR_SHA256
        )
        self.assertEqual(
            sha256(DITTO_EVALUATOR), judge_evaluator.FORK_EVALUATOR_SHA256
        )

    def test_revisions_are_full_length_and_distinct(self):
        self.assertEqual(len(judge_evaluator.UPSTREAM_REVISION), 40)
        self.assertEqual(len(judge_evaluator.FORK_REVISION), 40)
        self.assertNotEqual(
            judge_evaluator.UPSTREAM_REVISION, judge_evaluator.FORK_REVISION
        )


class ProductionParityTests(unittest.TestCase):
    """The rehearsal judge must BE the production judge, not resemble it.

    judge_evaluator.py says it mirrors provider_runtime.go. An unchecked
    comment is how the divergence this fork closes appeared in the first place:
    production moved to a new judge and the rehearsal kept scoring with the old
    one, with nothing failing. These read the Go constants and compare.
    """

    def setUp(self):
        self.constants = go_judge_constants()
        self.assertTrue(
            PROVIDER_RUNTIME.is_file(), f"missing {PROVIDER_RUNTIME}"
        )

    def test_every_judge_constant_was_found(self):
        # A regex that silently matched nothing would make every comparison
        # below vacuous, so the parse is asserted before it is trusted.
        for name in (
            "officialJudgeModel",
            "officialJudgeMaxTokens",
            "officialJudgeRevision",
            "officialJudgeRouteProvider",
            "officialJudgeReceiptProvider",
        ):
            self.assertIn(name, self.constants)

    def test_the_rehearsal_judge_matches_the_production_lane(self):
        self.assertEqual(
            {
                "model": self.constants["officialJudgeModel"],
                "max_tokens": self.constants["officialJudgeMaxTokens"],
                "revision": self.constants["officialJudgeRevision"],
                "route_provider": self.constants["officialJudgeRouteProvider"],
                "receipt_provider": self.constants["officialJudgeReceiptProvider"],
            },
            {
                "model": judge_evaluator.JUDGE_MODEL,
                "max_tokens": judge_evaluator.JUDGE_MAX_COMPLETION_TOKENS,
                "revision": judge_evaluator.JUDGE_PROFILE_REVISION,
                "route_provider": judge_evaluator.JUDGE_ROUTE_PROVIDER,
                "receipt_provider": judge_evaluator.JUDGE_RECEIPT_PROVIDER,
            },
            "the rehearsal judge and the production judge lane have diverged; "
            "miners would rehearse against a grader that does not score them",
        )

    def test_the_provider_block_matches_the_production_lane(self):
        """A subset of the production block is rejected, not widened.

        `require_parameters` drops endpoints that would ignore the completion
        bound, and `data_collection: deny` is part of what makes Together the
        eligible provider at all, so omitting either is a behaviour change.
        """
        route = self.constants["officialJudgeRouteProvider"]
        expected = go_provider_block(route)
        self.assertEqual(
            set(expected),
            {
                "only",
                "order",
                "allow_fallbacks",
                "require_parameters",
                "data_collection",
                "zdr",
            },
            "the Go provider block changed shape; re-check the Python helper",
        )
        # zdr is in the Go block itself now. The judge lane may address
        # OpenRouter directly, where the platform proxy is not there to append
        # it and the block is the only thing constraining the endpoint, so a
        # missing zdr is a real weakening rather than a mirroring artefact.
        self.assertIs(expected["zdr"], True, "the Go judge lane must request ZDR")
        self.assertEqual(dict(judge_evaluator.judge_provider_routing()), expected)

    def test_the_forked_evaluator_can_run_the_production_judge(self):
        """Parity is worthless if the evaluator cannot invoke that judge."""
        zoo = module_constant(DITTO_EVALUATOR, "model_zoo")
        bounds = module_constant(DITTO_EVALUATOR, "max_tokens_zoo")
        production_model = self.constants["officialJudgeModel"]
        entries = [name for name, e in zoo.items() if e[0] == production_model]
        self.assertEqual(
            len(entries), 1, f"{production_model} is not registered in model_zoo"
        )
        self.assertEqual(
            bounds.get(entries[0]), self.constants["officialJudgeMaxTokens"]
        )


class PromptIdentityTests(unittest.TestCase):
    """The comparability anchor: the fork must not touch the prompts."""

    def test_prompt_builder_is_byte_identical(self):
        upstream_source, upstream_templates = prompt_function(UPSTREAM_EVALUATOR)
        ditto_source, ditto_templates = prompt_function(DITTO_EVALUATOR)
        self.assertEqual(len(upstream_templates), 5)
        self.assertEqual(upstream_templates, ditto_templates)
        self.assertEqual(upstream_source, ditto_source)

    def test_the_off_by_one_clause_survives_verbatim(self):
        _, templates = prompt_function(DITTO_EVALUATOR)
        clause = (
            "do not penalize off-by-one errors for the number of days"
        )
        self.assertEqual(
            sum(clause in template for template in templates),
            1,
            "the temporal-reasoning rubric clause must be present exactly once",
        )


class DeltaTests(unittest.TestCase):
    def test_the_judge_is_registered_with_the_production_model_id(self):
        upstream_zoo = module_constant(UPSTREAM_EVALUATOR, "model_zoo")
        ditto_zoo = module_constant(DITTO_EVALUATOR, "model_zoo")
        self.assertNotIn(judge_evaluator.JUDGE_SHORT_NAME, upstream_zoo)
        self.assertIn(judge_evaluator.JUDGE_SHORT_NAME, ditto_zoo)
        self.assertEqual(
            ditto_zoo[judge_evaluator.JUDGE_SHORT_NAME][0], judge_evaluator.JUDGE_MODEL
        )
        for name, entry in upstream_zoo.items():
            self.assertEqual(ditto_zoo[name], entry, f"{name} must be untouched")

    def test_the_completion_bound_is_raised_only_for_the_new_judge(self):
        bounds = module_constant(DITTO_EVALUATOR, "max_tokens_zoo")
        self.assertEqual(
            bounds[judge_evaluator.JUDGE_SHORT_NAME],
            judge_evaluator.JUDGE_MAX_COMPLETION_TOKENS,
        )
        self.assertNotIn(judge_evaluator.OFFICIAL_JUDGE_SHORT_NAME, bounds)


class JudgeCallTests(unittest.TestCase):
    def test_the_fork_sends_the_frozen_bound_and_call_shape(self):
        run = run_evaluator(
            DITTO_EVALUATOR, judge_evaluator.JUDGE_SHORT_NAME, content="yes"
        )
        self.assertEqual(len(run.client.calls), 1)
        call = run.client.calls[0]
        self.assertEqual(call["model"], judge_evaluator.JUDGE_MODEL)
        self.assertEqual(
            call["max_tokens"], judge_evaluator.JUDGE_MAX_COMPLETION_TOKENS
        )
        self.assertEqual(call["temperature"], 0)
        self.assertEqual(call["n"], 1)

        self.assertEqual(len(run.rows), 1)
        self.assertEqual(
            run.rows[0]["autoeval_label"],
            {"model": judge_evaluator.JUDGE_MODEL, "label": True},
        )

    def test_the_official_judge_keeps_upstreams_bound_in_the_fork(self):
        run = run_evaluator(
            DITTO_EVALUATOR,
            judge_evaluator.OFFICIAL_JUDGE_SHORT_NAME,
            content="yes",
        )
        self.assertEqual(
            run.client.calls[0]["max_tokens"],
            judge_evaluator.OFFICIAL_JUDGE_MAX_COMPLETION_TOKENS,
        )

    def test_the_result_file_is_named_for_the_judge_that_ran(self):
        run = run_evaluator(
            DITTO_EVALUATOR, judge_evaluator.JUDGE_SHORT_NAME, content="yes"
        )
        self.assertTrue(
            run.result_name.endswith(
                judge_evaluator.evaluation_suffix(judge_evaluator.JUDGE_SHORT_NAME)
            ),
            run.result_name,
        )
        self.assertNotIn(
            judge_evaluator.OFFICIAL_JUDGE_SHORT_NAME, run.result_name
        )

    def test_the_fork_refuses_a_truncated_verdict(self):
        with self.assertRaises(RuntimeError) as caught:
            run_evaluator(
                DITTO_EVALUATOR,
                judge_evaluator.JUDGE_SHORT_NAME,
                content="",
                finish_reason="length",
            )
        self.assertIn("empty completion", str(caught.exception))
        self.assertIn("length", str(caught.exception))

    def test_upstream_silently_scores_a_truncated_verdict_no(self):
        """The bug the fork exists to fix, pinned so the fix cannot regress.

        If this ever stops producing a silent False, the premise of the fork
        changed and the comparison in every report built on it is stale.
        """
        run = run_evaluator(
            UPSTREAM_EVALUATOR,
            judge_evaluator.OFFICIAL_JUDGE_SHORT_NAME,
            content="",
            finish_reason="length",
        )
        self.assertEqual(len(run.rows), 1)
        self.assertIs(
            run.rows[0]["autoeval_label"]["label"],
            False,
            "upstream is expected to label a truncated verdict False, silently",
        )


if __name__ == "__main__":
    unittest.main()
