"""The decision-record enumerations have one source: the protocol Literals.

``ScreeningDecisionRecord`` derives its CHECK constraints from
``ditto_screening_protocol`` ``get_args``; the Alembic migration carries the
frozen snapshot of the same values. Pinning the two equal means a new outcome
or failure domain cannot land in the model without a follow-up migration, and
the migration text never drifts from what the model enforces.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast, get_args

from sqlalchemy import CheckConstraint, Table

from ditto.db.models import ScreeningDecisionRecord
from ditto_screening_protocol import FailureDomain, ScreeningDecisionOutcome

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "2026_09_14_add_screening_decision_records.py"
)
_CHECK = re.compile(
    r'sa\.CheckConstraint\(\s*((?:"[^"]*"\s*)+),\s*name="([^"]+)"', re.S
)


def _model_check(name: str) -> str:
    # The metadata naming convention prefixes ``ck_<table>_``; the migration
    # names the constraint bare, so match on the suffix.
    table = cast(Table, ScreeningDecisionRecord.__table__)
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and str(constraint.name).endswith(
            name
        ):
            return str(constraint.sqltext)
    raise AssertionError(f"model lost CHECK {name}")


def _migration_checks() -> dict[str, str]:
    source = _MIGRATION.read_text()
    checks: dict[str, str] = {}
    for match in _CHECK.finditer(source):
        # Adjacent string literals are concatenated by the compiler; join them
        # the same way before comparing against the model's rendered text.
        checks[match.group(2)] = "".join(re.findall(r'"([^"]*)"', match.group(1)))
    assert checks, "migration lost its CheckConstraints"
    return checks


def test_model_enum_checks_match_the_frozen_migration() -> None:
    migration = _migration_checks()
    for name in (
        "screening_decision_records_outcome_check",
        "screening_decision_records_failure_domain_check",
    ):
        assert _model_check(name) == migration[name], name


def test_model_checks_enumerate_the_protocol_literals() -> None:
    outcome = _model_check("screening_decision_records_outcome_check")
    domain = _model_check("screening_decision_records_failure_domain_check")
    assert [f"'{v}'" in outcome for v in get_args(ScreeningDecisionOutcome)] == [
        True
    ] * len(get_args(ScreeningDecisionOutcome))
    assert [f"'{v}'" in domain for v in get_args(FailureDomain)] == [True] * len(
        get_args(FailureDomain)
    )
