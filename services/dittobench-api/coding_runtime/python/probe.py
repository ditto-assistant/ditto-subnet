"""Synthetic image-only regression fixture, absent from the runtime target."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

case = sys.argv[1]
suite = """from demo import Counter
def test_add() -> None:
    value = Counter(3)
    assert value.add(2) == 5
    assert value.add(4) == 9
def test_fresh():
    value = Counter(3)
    assert value.add(0) == 3
"""


def denied(statement):
    return (
        "import os\n        try:\n            "
        + statement
        + "\n            return 999\n        except PermissionError: pass"
    )


attacks = {
    "pass": "pass",
    "visible": "pass",
    "wrong": "return 999",
    "stdout": 'print("ordinary candidate diagnostic", flush=True)',
    "early-exit": "import os; os._exit(0)",
    "fake-report": (
        'print(\'{"passed":2,"total":2,"completed":true}\', flush=True); return 999'
    ),
    "oversized": 'import os; os.write(3, b"x" * 70000); return 999',
    "hang": "while True: pass",
    "hidden-read": denied('open("/run/dittobench-grader/suite.py").read()'),
    "report-write": denied(
        'open("/run/dittobench-control/test-report.json", "w").write("forged")'
    ),
    "fork": denied("os.fork()"),
    "threads": (
        "import threading\n        values = []\n"
        "        t = threading.Thread(target=lambda: values.append(1))\n"
        "        t.start()\n        t.join()\n"
        "        if values != [1]: return 999"
    ),
    "exec": denied('os.execv("/usr/local/bin/python3", ["python3", "-c", "pass"])'),
    "setsid": denied("os.setsid()"),
    "setuid": denied("os.setuid(0)"),
    "capabilities": (
        'status = open("/proc/self/status").read()\n'
        '        caps = status.split("CapEff:\\t", 1)[1].splitlines()[0]\n'
        "        if int(caps, 16) != 0: return 999"
    ),
    "environment": 'import os\n        if "SYNTHETIC_SECRET" in os.environ: return 999',
    "tuple": "return (5,)",
    "bytes": "return amount",
}
if case not in attacks and case not in {"unsupported", "count-mismatch"}:
    raise ValueError("unknown synthetic scenario")
attack = attacks.get(case, "pass")
source = f"""class Counter:
    def __init__(self, value): self.value = value
    def add(self, amount):
        {attack}
        self.value += amount
        return self.value
"""
if case == "tuple":
    suite = (
        "from demo import Counter\ndef test_tuple():\n"
        "    assert Counter(0).add(0) == [5]\n"
    )
if case == "bytes":
    suite = (
        "from demo import Counter\ndef test_bytes() -> None:\n"
        "    assert Counter(0).add({'x': [b'\\x00\\xff']}) == {'x': [b'\\x00\\xff']}\n"
    )
if case == "unsupported":
    suite += "\ndef test_loop():\n    for x in [1]:\n        assert x == 1\n"
workspace = Path("/workspace")
control = Path("/run/dittobench-control")
grader = Path("/run/dittobench-grader")
for root in (workspace, control, grader):
    root.mkdir(parents=True, exist_ok=True)
control.chmod(0o700)
grader.chmod(0o700)
(workspace / "demo.py").write_text(source)
(workspace / "visible.py").write_text(suite)
(grader / "suite.py").write_text(suite)
for path in (workspace / "demo.py", workspace / "visible.py", grader / "suite.py"):
    path.chmod(0o444)
total = 1 if case in {"tuple", "bytes"} else 2
if case == "count-mismatch":
    total = 3
argv = [
    "dittobench-test-driver",
    "--group",
    "visible" if case == "visible" else "hidden",
    "--suite",
    "visible.py" if case == "visible" else "suite.py",
    "--candidate-timeout-ms",
    "1000",
    "--module",
    "demo",
]
command = {"argv": argv, "id": "synthetic", "timeout_milliseconds": 15000}
digest = hashlib.sha256(
    (json.dumps(command, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
request = {
    "schema": "dittobench-coding-supervisor-request-v1",
    "nonce": "a" * 48,
    "mode": "test",
    "command_id": "synthetic",
    "command_sha256": digest,
    "argv": argv,
    "timeout_milliseconds": 15000,
    "expected_total": total,
    "candidate_uid": 10001,
    "candidate_gid": 10001,
}
(control / "request.json").write_text(json.dumps(request))
(control / "request.json").chmod(0o400)
result = subprocess.run(
    [
        "/usr/local/bin/dittobench-coding-supervisor",
        "--request",
        str(control / "request.json"),
        "--response",
        str(control / "response.json"),
    ],
    capture_output=True,
    timeout=25,
)
invalid = case in {"unsupported", "count-mismatch"}
if invalid:
    assert result.returncode != 0 and not (control / "test-report.json").exists()
else:
    if result.returncode != 0:
        # Only this synthetic image contains the diagnostic harness. Production
        # driver stderr stays discarded by the supervisor.
        import importlib.machinery

        module = importlib.machinery.SourceFileLoader(
            "synthetic_driver_debug", "/usr/local/bin/dittobench-test-driver"
        ).load_module()
        sys.argv = [
            "driver",
            *argv[1:],
            "--dittobench-report",
            str(control / "test-report.json"),
            "--dittobench-nonce",
            "a" * 48,
            "--dittobench-expected",
            str(total),
            "--dittobench-candidate-uid",
            "10001",
            "--dittobench-candidate-gid",
            "10001",
        ]
        module.main()
    assert result.returncode == 0, "synthetic supervisor failed"
    report = json.loads((control / "response.json").read_bytes())
    expected = (
        0
        if case in {"wrong", "early-exit", "fake-report", "oversized", "hang", "tuple"}
        else total
    )
    assert report["passed"] == expected and report["total"] == total, (case, report)
    assert report["completed"] and report["process_tree_dead"]
    assert not report["stdout"] and not report["stderr"]
print("synthetic probe passed: " + case)
