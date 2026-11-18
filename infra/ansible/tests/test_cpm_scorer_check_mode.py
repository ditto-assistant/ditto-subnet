#!/usr/bin/env python3
"""The CPM scorer's bearer reads survive --check, and its drain survives a race.

Run with ansible-core (infra-ci: `uvx --from ansible-core==2.21.2 python3`).
The real task files are loaded, and the real `when`, `changed_when` and
`failed_when` expressions are evaluated with Ansible's own templar. No host, no
cloud, no secret.
"""

import unittest
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROLES = Path(__file__).resolve().parents[1] / "roles"
CPM = ROLES / "cpm_scorer" / "tasks"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _task(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


def _evaluate(expressions, variables: dict) -> bool:
    templar = Templar(loader=DataLoader(), variables=variables)
    if isinstance(expressions, str):
        expressions = [expressions]
    # A `when` list is a short-circuiting AND, as Ansible evaluates it.
    for expression in expressions:
        if not templar.evaluate_conditional(trust_as_template(str(expression))):
            return False
    return True


class BearerReadsRunInCheckMode(unittest.TestCase):
    """Each read only reads, so it runs under --check, never logs, never changes."""

    READS = [
        (CPM / "token-file.yml", "Read the Platform bearer from the host file"),
        (
            CPM / "token-aws-wif.yml",
            "Read the Platform bearer through the federated instance role",
        ),
        (
            ROLES / "platform_app" / "tasks" / "main.yml",
            "Read the model-context-scorer bearer from Secret Manager",
        ),
        (
            ROLES / "platform_app" / "tasks" / "main.yml",
            "Read the generator-scorer bearer from Secret Manager",
        ),
    ]

    def test_reads_are_check_mode_safe_and_silent(self) -> None:
        for path, name in self.READS:
            with self.subTest(task=name):
                task = _task(path, name)
                self.assertIs(task.get("check_mode"), False)
                self.assertIs(task.get("no_log"), True)
                self.assertIs(task.get("changed_when"), False)
                self.assertIn("ansible.builtin.command", task)

    def test_every_consumer_of_the_bearer_is_silent(self) -> None:
        for name in (
            "Validate the Platform bearer before writing it",
            "Write the Platform bearer file",
        ):
            with self.subTest(task=name):
                self.assertIs(_task(CPM / "main.yml", name).get("no_log"), True)
        platform = ROLES / "platform_app" / "tasks" / "main.yml"
        for name in (
            "Fold the model-context-scorer bearer into the secrets map",
            "Validate the model-context-scorer bearer",
        ):
            with self.subTest(task=name):
                self.assertIs(_task(platform, name).get("no_log"), True)

    def test_wif_read_never_needs_a_dry_run_credential_file(self) -> None:
        read = _task(
            CPM / "token-aws-wif.yml",
            "Read the Platform bearer through the federated instance role",
        )
        cases = {
            # (check mode, credential configuration exists) -> read runs
            (False, False): True,  # a real converge always reads
            (False, True): True,
            (True, True): True,  # a dry run reads with the existing config
            (True, False): False,  # a fresh host's dry run skips, writes nothing
        }
        for (check, exists), expected in cases.items():
            with self.subTest(check=check, exists=exists):
                self.assertIs(
                    _evaluate(
                        read["when"],
                        {
                            "ansible_check_mode": check,
                            "cpm_scorer_wif_credential": {"stat": {"exists": exists}},
                        },
                    ),
                    expected,
                )
        # The render that creates the config stays a normal (skipped in --check)
        # command: a dry run never writes it.
        render = _task(
            CPM / "token-aws-wif.yml",
            "Render the non-secret AWS external-account configuration",
        )
        self.assertNotIn("check_mode", render)

    def test_skipped_read_skips_validation_and_bearer_file(self) -> None:
        skipped = {"cpm_scorer_token_read": {"skipped": True, "changed": False}}
        read = {"cpm_scorer_token_read": {"stdout": "x" * 64, "changed": False}}
        for name in (
            "Validate the Platform bearer before writing it",
            "Write the Platform bearer file",
        ):
            with self.subTest(task=name):
                when = _task(CPM / "main.yml", name)["when"]
                self.assertFalse(_evaluate(when, skipped))
                self.assertTrue(_evaluate(when, read))


class DrainToleratesOnlyAVanishedContainer(unittest.TestCase):
    def setUp(self) -> None:
        self.drain = _task(
            CPM / "main.yml", "Drain the scorer onto the new bearer or unit"
        )
        self.ensure = _task(
            CPM / "main.yml", "Ensure the scorer restarts onto the new bearer or unit"
        )

    def _result(self, rc: int, stderr: str) -> dict:
        return {"cpm_scorer_drain": {"rc": rc, "stderr": stderr, "changed": False}}

    def test_outcomes(self) -> None:
        cases = [
            # (rc, stderr, failed, changed, ensure-started runs)
            (0, "", False, True, False),
            (
                1,
                "Error response from daemon: No such container: ditto-cpm-scorer",
                False,
                False,
                True,
            ),
            (
                1,
                "Error response from daemon: cannot kill container: "
                "ditto-cpm-scorer: container 0123 is not running",
                False,
                False,
                True,
            ),
            (
                1,
                "permission denied while trying to connect to the Docker daemon",
                True,
                False,
                True,
            ),
        ]
        for rc, stderr, failed, changed, ensure in cases:
            with self.subTest(stderr=stderr):
                variables = self._result(rc, stderr)
                self.assertIs(_evaluate(self.drain["failed_when"], variables), failed)
                self.assertIs(_evaluate(self.drain["changed_when"], variables), changed)
                self.assertIs(_evaluate(self.ensure["when"], variables), ensure)

    def test_skipped_drain_leaves_the_unit_alone(self) -> None:
        skipped = {"cpm_scorer_drain": {"skipped": True, "changed": False}}
        self.assertFalse(_evaluate(self.ensure["when"], skipped))
        self.assertEqual(
            self.ensure["ansible.builtin.systemd"]["state"], "started"
        )  # start, never restart: a live drain is not interrupted


if __name__ == "__main__":
    unittest.main()
