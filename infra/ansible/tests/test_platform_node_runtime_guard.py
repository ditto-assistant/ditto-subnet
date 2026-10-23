"""Run the production admission task through Ansible against version fixtures."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
TASKS = ROOT / "infra/ansible/roles/platform_app/tasks/node_runtime_guard.yml"


class PlatformNodeAdmissionTests(unittest.TestCase):
    def test_distro_and_mapped_daemon_version_boundaries(self):
        executable = shutil.which("ansible-playbook")
        if executable is None:
            self.skipTest("ansible-playbook is required to exercise the actual task")
        admission = yaml.safe_load(TASKS.read_text())[-1]
        cases = [
            ("fresh host", None, [], False),
            ("Node 20 distro", "v20.20.0", [], True),
            ("unfixed 22", "v22.23.3", [], True),
            ("fixed 22", "v22.23.4", [], False),
            ("newer 22", "v22.24.0", [], False),
            ("unfixed 24", "v24.22.0", [], True),
            ("fixed 24", "v24.22.1", [], False),
            ("unfixed 26", "v26.12.0", [], True),
            ("fixed 26", "v26.12.1", [], False),
            ("EOL 25", "v25.9.0", [], True),
            ("unreviewed 27", "v27.0.0", [], True),
            ("prerelease", "v24.22.1-rc.1", [], True),
            ("invalid version", "not-node", [], True),
            ("old mapped daemon behind fixed distro", "v24.22.1", ["v24.20.0"], True),
            ("old mapped daemon without distro", None, ["v22.23.3"], True),
            ("all fixed mapped daemons", "v22.23.4", ["v24.22.1", "v26.12.1"], False),
        ]
        tasks = []
        for name, distro, daemons, should_fail in cases:
            task = copy.deepcopy(admission)
            task["name"] = name
            task["vars"].update(
                platform_node_distro_version={"stdout": distro, "cmd": ["/usr/bin/node", "--version"]} if distro else {"skipped": True},
                platform_node_pm2_versions={"results": [{"stdout": version, "cmd": [f"/proc/{index + 100}/exe", "--version"]} for index, version in enumerate(daemons)]},
            )
            task["ignore_errors"] = True
            task["register"] = "node_admission_case"
            tasks.extend([task, {"name": f"Check {name}", "ansible.builtin.assert": {"that": [f"node_admission_case is failed == {'true' if should_fail else 'false'}"]}}])
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / "cases.yml"
            playbook.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "tasks": tasks}], sort_keys=False))
            environment = dict(os.environ, ANSIBLE_NOCOLOR="1")
            result = subprocess.run([executable, "-i", "localhost,", "--connection", "local", str(playbook)], env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
