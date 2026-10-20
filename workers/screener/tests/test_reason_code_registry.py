"""The worker's reason codes agree with the shared screening registry.

Platform decides from a reason code whether a failed attempt retries
automatically, backs off, or parks for an operator, and it reads that
classification from ``ditto_screening_protocol.reason_codes``. These checks
fail when the worker drifts from it: a registered worker code nothing emits
any more (Platform would keep a dead retry or backoff entry, as it did for
``source-review-retryable-infra`` until #2518), or a new infrastructure exit
whose code Platform has never classified.

The scan is static. A code counts as emitted when the worker source spells it
as an exact string literal or names its registry constant, which is how every
registered code is produced today.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

from ditto_screening_protocol import reason_codes
from ditto_screening_protocol.reason_codes import (
    INFRA_AUTO_RETRY_REASON_CODES,
    PROVIDER_BACKOFF_REASON_CODES,
    SCREENING_REASON_CODES,
    SEED_PROBE_REASON_CODES,
    ReasonCodeClass,
    ReasonCodeProducer,
)

PACKAGE = Path(__file__).resolve().parents[1] / "ditto_screener"
REGISTRY_MODULE = "ditto_screening_protocol.reason_codes"
_INFRA_CLASSES = frozenset(
    {
        ReasonCodeClass.FLEET_INFRA,
        ReasonCodeClass.PROVIDER_BACKOFF,
        ReasonCodeClass.OPERATOR_RETRY,
    }
)
# ``seed-*`` strings that are probe observations, never a rejection reason:
# ``seed-ok`` is a passing probe and ``seed-envelope-usage`` is shadow headroom
# appended after the deciding evidence (``worker._SEED_ENVELOPE_OBSERVATION``).
_SEED_OBSERVATIONS = frozenset({"seed-ok", "seed-envelope-usage"})
# A whole code, not an f-string fragment such as ``"seed-"``.
_SEED_CODE = re.compile(r"seed-[a-z0-9]+(?:-[a-z0-9]+)*")


def _registry_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(reason_codes).items()
        if not name.startswith("_")
        and type(value) is str
        and value in SCREENING_REASON_CODES
    }


@cache
def _modules() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(PACKAGE.rglob("*.py"))
    )


def _registry_names(tree: ast.Module) -> dict[str, str]:
    """Local name -> code for registry constants ``tree`` imports or aliases.

    A module-level alias such as ``CLAIM_NOT_STARTED_REASON_CODE =
    WORKER_CLAIM_NOT_STARTED`` resolves too.
    """
    constants = _registry_constants()
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == REGISTRY_MODULE:
            for alias in node.names:
                if alias.name in constants:
                    names[alias.asname or alias.name] = constants[alias.name]
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Name)
            and statement.value.id in names
        ):
            names[statement.targets[0].id] = names[statement.value.id]
    return names


def _code_values(node: ast.expr, names: dict[str, str]) -> set[str]:
    """The literal codes an expression can evaluate to (both IfExp arms)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in names:
        return {names[node.id]}
    if isinstance(node, ast.IfExp):
        return _code_values(node.body, names) | _code_values(node.orelse, names)
    return set()


@cache
def _emitted_codes() -> frozenset[str]:
    """Every exact string literal, plus every registry constant referenced."""
    found: set[str] = set()
    for _path, tree in _modules():
        names = _registry_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
            elif isinstance(node, ast.Name) and node.id in names:
                found.add(names[node.id])
    return frozenset(found)


def _is_retryable_infra(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "RETRYABLE_INFRA"


def _retryable_infra_exit_codes() -> dict[str, list[str]]:
    """Codes on decisions whose outcome is spelled ``RETRYABLE_INFRA``.

    Covers ``core_decision(ScreeningOutcome.RETRYABLE_INFRA, code=...)``, with
    a conditional outcome taking the matching arm of a conditional code, and
    the claim failures ``_submit_claim_failure(reason_code=...)`` always
    submits as ``retryable_infra``. Codes computed at runtime (an
    ``observation.error_code``, ``l3-critic-{code}``) are out of reach of a
    static scan and stay unclassified, which Platform parks for an operator.
    """
    sites: dict[str, list[str]] = {}

    def record(code: str, path: Path, node: ast.AST) -> None:
        sites.setdefault(code, []).append(
            f"{path.relative_to(PACKAGE)}:{getattr(node, 'lineno', '?')}"
        )

    for path, tree in _modules():
        names = _registry_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "fallback_reason"
                for target in node.targets
            ):
                for code in _code_values(node.value, names):
                    record(code, path, node)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else "")
            )
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if func_name == "_submit_claim_failure" and "reason_code" in keywords:
                for code in _code_values(keywords["reason_code"], names):
                    record(code, path, node)
                continue
            if not node.args or "code" not in keywords:
                continue
            outcome, code_expr = node.args[0], keywords["code"]
            if _is_retryable_infra(outcome):
                codes = _code_values(code_expr, names)
            elif isinstance(outcome, ast.IfExp) and isinstance(code_expr, ast.IfExp):
                arm = (
                    code_expr.body
                    if _is_retryable_infra(outcome.body)
                    else code_expr.orelse
                    if _is_retryable_infra(outcome.orelse)
                    else None
                )
                codes = set() if arm is None else _code_values(arm, names)
            else:
                codes = set()
            for code in codes:
                record(code, path, node)
    return sites


def test_the_scan_sees_the_worker_package() -> None:
    assert len(_modules()) > 20
    assert "docker-build" in _emitted_codes()
    assert _registry_names(dict(_modules())[PACKAGE / "worker.py"])


@pytest.mark.parametrize(
    "code",
    [
        code
        for code, entry in SCREENING_REASON_CODES.items()
        if entry.producer == ReasonCodeProducer.WORKER
    ],
)
def test_every_registered_worker_code_is_still_emitted(code: str) -> None:
    """A retry, backoff or public entry for a code nothing produces is dead."""
    if code not in _emitted_codes():
        pytest.fail(
            f"{code!r} is registered as a worker code but no worker module "
            "emits it; mark it retired in ditto_screening_protocol.reason_codes "
            "or restore its producer"
        )


@pytest.mark.parametrize(
    "code",
    [*INFRA_AUTO_RETRY_REASON_CODES, *PROVIDER_BACKOFF_REASON_CODES],
)
def test_platform_retry_and_backoff_codes_are_worker_codes(code: str) -> None:
    entry = SCREENING_REASON_CODES[code]
    if entry.producer == ReasonCodeProducer.RETIRED:
        # Allowlisted in the registry's own tests (Targon/Cloud Run retired).
        if code in _emitted_codes():
            pytest.fail(f"retired {code!r} is emitted again; register it as live")
        return
    assert entry.producer == ReasonCodeProducer.WORKER
    if code not in _emitted_codes():
        pytest.fail(f"Platform retries or backs off {code!r}; no worker emits it")


@pytest.mark.parametrize(
    "code",
    [
        code
        for code, entry in SCREENING_REASON_CODES.items()
        if entry.producer != ReasonCodeProducer.WORKER
    ],
)
def test_worker_never_emits_platform_or_retired_codes(code: str) -> None:
    if code in _emitted_codes():
        pytest.fail(f"the worker now emits {code!r}; register it as a worker code")


def test_every_retryable_infra_exit_is_classified_as_infrastructure() -> None:
    sites = _retryable_infra_exit_codes()
    # Guard the scan itself: these exits exist today.
    assert {
        "docker-build-infrastructure",
        "worker-claim-not-started",
        "worker-platform-request-failed",
        "unexpected-infrastructure",
    } <= sites.keys()
    unclassified = {
        code: where
        for code, where in sites.items()
        if code not in SCREENING_REASON_CODES
        or SCREENING_REASON_CODES[code].classification not in _INFRA_CLASSES
    }
    assert not unclassified, (
        "register these retryable_infra codes in "
        f"ditto_screening_protocol.reason_codes: {unclassified}"
    )


def test_every_seed_rejection_is_a_registered_seed_probe_code() -> None:
    worker_seed_codes = {
        code
        for code in _emitted_codes()
        if _SEED_CODE.fullmatch(code) and code not in _SEED_OBSERVATIONS
    }
    assert worker_seed_codes == set(SEED_PROBE_REASON_CODES)


def test_worker_constants_are_the_registry_codes() -> None:
    from ditto_screener.policy import SOURCE_REVIEW_KEY_UNAVAILABLE_CODE
    from ditto_screener.worker import CLAIM_NOT_STARTED_REASON_CODE

    assert CLAIM_NOT_STARTED_REASON_CODE == reason_codes.WORKER_CLAIM_NOT_STARTED
    assert (
        SOURCE_REVIEW_KEY_UNAVAILABLE_CODE
        == reason_codes.SOURCE_REVIEW_ADJUDICATOR_KEY_UNAVAILABLE
    )
