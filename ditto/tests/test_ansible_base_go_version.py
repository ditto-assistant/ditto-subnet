"""Static guards for the base role's Go version detection (SN118 incident 2026-10-27).

`regex_search(..., '\\1')` inside a folded scalar raised
"'NoneType' object has no attribute 'group'" on ansible-core 2.21 and blocked
every host that converges `base`. infra-ci evaluates the real task against
stubbed outputs; these checks keep that test wired and the pattern gone.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "infra" / "ansible"
INFRA_CI = ROOT / ".github" / "workflows" / "infra-ci.yml"
BACKREFERENCE_REGEX_FILTER = re.compile(r"regex_search\([^)]*,\s*['\"]\\{1,2}\d")


def _ansible_core_pins(text: str) -> set[str]:
    return set(re.findall(r"ansible-core==([0-9.]+)", text))


def test_go_version_detection_lives_in_one_regex_free_task_file() -> None:
    go_tasks = yaml.safe_load((ANSIBLE / "roles/base/tasks/go.yml").read_text())
    imports = [t.get("ansible.builtin.import_tasks") for t in go_tasks]
    assert "go_version_fact.yml" in imports
    assert not any("ansible.builtin.set_fact" in t for t in go_tasks)

    fact_path = ANSIBLE / "roles/base/tasks/go_version_fact.yml"
    fact_tasks = yaml.safe_load(fact_path.read_text())
    assert len(fact_tasks) == 1
    expression = fact_tasks[0]["ansible.builtin.set_fact"]["current_go_version"]
    assert "regex" not in expression
    assert "\\" not in expression


def test_no_ansible_yaml_uses_a_regex_search_backreference_group() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in sorted(ANSIBLE.rglob("*.yml"))
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if BACKREFERENCE_REGEX_FILTER.search(line)
    ]
    assert offenders == []


def test_infra_ci_evaluates_the_real_task_under_the_pinned_ansible_core() -> None:
    text = INFRA_CI.read_text()
    workflow = yaml.safe_load(text)
    steps = workflow["jobs"]["ansible"]["steps"]
    runs = [step.get("run", "") for step in steps]
    matching = [run for run in runs if "tests/base-go-version.yml" in run]
    assert len(matching) == 1
    pins = _ansible_core_pins(text)
    assert len(pins) == 1, f"infra-ci pins more than one ansible-core: {pins}"
    assert _ansible_core_pins(matching[0]) == pins
    assert "ansible-playbook -i localhost," in matching[0]

    test_playbook = yaml.safe_load((ANSIBLE / "tests/base-go-version.yml").read_text())
    case_file = yaml.safe_load((ANSIBLE / "tests/base-go-version-case.yml").read_text())
    included = [
        task["ansible.builtin.include_role"]
        for task in [*test_playbook[0]["tasks"], *case_file]
        if "ansible.builtin.include_role" in task
    ]
    assert included
    assert all(
        role == {"name": "base", "tasks_from": "go_version_fact"} for role in included
    )
    cases = test_playbook[0]["vars"]["go_version_cases"]
    expectations = {case["expected"] for case in cases}
    assert "" in expectations
    assert any(expected for expected in expectations)
