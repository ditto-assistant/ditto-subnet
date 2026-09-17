"""Native pre-exec confinement collector (B5 PR6) against a simulated host.

Nothing here touches a host or daemon. ``PreexecFakeHost`` models the pre-exec
agent, the production grading container each fixture runs in, and the
candidate's ``/proc`` as the kernel reports it from outside. A correct host
yields a record the offline verifier accepts; each knob turns one outside
observation and must either make the verifier refuse the record or make the
collector refuse outright.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from ditto.tests import test_coding_native_enforcement_evidence as base
from ditto.tests import test_coding_native_resource_collector as resource

COLLECTOR = resource.COLLECTOR
UID, GID = resource.UID, resource.GID
SUBUID = resource.SUBUID
AGENT_PID = resource.AGENT_PID
CANDIDATE = 10001
FIXTURES = base.PREEXEC_FIXTURES
FIXTURES_RAW = base.PREEXEC_FIXTURES_RAW
CONTROLS = ("hang", "pass", "wrong")
LANGUAGES = base.EVIDENCE.LANGUAGES
CAPABILITY_SETS = (
    "ambient",
    "bounding",
    "effective",
    "inheritable",
    "permitted",
)


class PreexecScenario(resource.Scenario):
    """Knobs a refusal test turns; the defaults describe a confined host."""

    def __init__(self) -> None:
        super().__init__()
        # Fixtures whose suite fails: the subject was not confined.
        self.unconfined: set[tuple[str, str]] = set()
        # Fixtures whose run times out.
        self.timed_out: set[tuple[str, str]] = set()
        self.build_failed: set[tuple[str, str]] = set()
        self.capabilities = dict.fromkeys(CAPABILITY_SETS, 0)
        self.no_new_privs = 1
        self.seccomp_mode = 2
        self.candidate_uid = CANDIDATE
        self.control_total = 2
        self.wrong_passed = 1
        self.suite_digest: str | None = None
        self.subject_digest: str | None = None
        self.no_candidate = False


class PreexecAgentSession:
    """The agent's line protocol, as the collector speaks it."""

    def __init__(self, host: "PreexecFakeHost", arguments: list[str]) -> None:
        self.host, self.arguments = host, arguments
        self.closed = False
        self.counter = 0

    def read(self, timeout: float) -> dict:
        assert timeout > 0
        raise COLLECTOR.Refusal("agent did not answer in time")

    def request(self, value: dict, timeout: float) -> dict:
        assert timeout > 0
        host, scenario = self.host, self.host.scenario
        host.requests.append(value)
        op = value["op"]
        answer: dict[str, Any] = {"schema": COLLECTOR.PREEXEC_AGENT_SCHEMA, "op": op}
        if op == "hello":
            answer.update(
                pid=AGENT_PID,
                uid=scenario.agent_uid,
                gid=GID,
                probe_runner_binary_sha256=scenario.agent_digest,
                inputs=scenario.agent_inputs or host.world.inputs(),
            )
        elif op == "start":
            answer.update(host.start_fixture(value, f"p{self.counter}"))
            self.counter += 1
        elif op == "wait":
            answer.update(host.wait_fixture(value["run"]))
        elif op == "exit":
            host.agent_exited = True
        return answer

    def close(self) -> None:
        self.closed = True
        self.host.agent_exited = True


class PreexecFakeHost(resource.ResourceFakeHost):
    def __init__(self, world: "PreexecWorld", scenario: PreexecScenario) -> None:
        super().__init__(world, scenario)
        self.fixture_runs: dict[str, dict] = {}

    # -- the agent ----------------------------------------------------------

    def resource_session(self, unit: str, arguments: list[str]):
        self.agent_unit, self.agent_arguments = unit, arguments
        self.agent_exited = self.agent_terminated = self.agent_killed = False
        self.agent = PreexecAgentSession(self, arguments)
        return self.agent

    def cgroup_procs(self, cgroup: str) -> list[int]:
        if "ditto-native-preexec-agent" in cgroup:
            return [] if self.agent_exited else [AGENT_PID]
        return super().cgroup_procs(cgroup)

    # -- one fixture run ----------------------------------------------------

    def start_fixture(self, value: dict, run_id: str) -> dict:
        language, fixture = value["language"], value["fixture"]
        assert value["repository"] == f"coding-runtime.invalid/{language}/runtime"
        entry = FIXTURES["languages"][language]
        phase, _, name = fixture.partition(".")
        subject = (
            entry["controls"][name]["subject"]["sha256"]
            if phase == "control"
            else entry["hostile"][name]["subject"]["sha256"]
        )
        init = self.next_pid
        self.next_pid += 10
        run = {
            "id": run_id,
            "class": "executor_grading",
            "language": language,
            "fixture": fixture,
            "argv": ["workload", "hold"],
            "mode": "hold",
            "flags": {},
            "nonce": f"{init:016x}",
            "container_id": f"{init:064x}",
            "init": init,
            "supervisor": init + 1,
            "workload": init + 2,
            "child": init + 3,
            "started": self.mono,
            "timeout_ms": base.GROUP_TIMEOUTS_MS[entry["test_group"]],
            "fail_start": False,
            "instance": "coding-executor-" + f"{init:032x}",
            "network": f"ditto-job-{init:016x}",
        }
        self.runs[run_id] = run
        self.fixture_runs[run_id] = run
        for role in ("init", "supervisor", "workload", "child"):
            self.pids[run[role]] = (run, role)
        self.containers[run["container_id"]] = {"run": run_id}
        scenario = self.scenario
        return {
            "run": run_id,
            "executor_instance": run["instance"],
            "subject_sha256": scenario.subject_digest or subject,
            "suite_sha256": scenario.suite_digest or entry["controls_suite_sha256"],
            "command_timeout_ms": run["timeout_ms"],
        }

    def wait_fixture(self, run_id: str) -> dict:
        run = self.runs[run_id]
        self.mono = run["started"] + 1
        self.containers.pop(run["container_id"], None)
        scenario = self.scenario
        key = (run["language"], run["fixture"])
        answer: dict[str, Any] = {"run": run_id, "done": True, "return_code": 0}
        if key in scenario.build_failed:
            return {**answer, "build_failed": True, "passed": 0, "total": 0}
        if key in scenario.timed_out or run["fixture"] == "control.hang":
            return {
                **answer,
                "return_code": 124,
                "timed_out": True,
                "passed": 0,
                "total": scenario.control_total,
            }
        total = scenario.control_total
        if run["fixture"] == "control.wrong":
            passed = scenario.wrong_passed
        elif key in scenario.unconfined:
            passed = 0
        else:
            passed = total
        return {**answer, "completed": True, "passed": passed, "total": total}

    # -- /proc --------------------------------------------------------------

    def children(self, pid: int) -> list[int]:
        run, role = self.run_of_pid(pid)
        if run is None:
            return []
        if role == "init":
            return [run["supervisor"]]
        if role == "supervisor":
            return [run["workload"]]
        return []

    def proc(self, pid: int, name: str) -> bytes:
        if pid == AGENT_PID and name == "cgroup":
            return (
                b"0::/user.slice/user-1001.slice/user@1001.service/app.slice/"
                b"ditto-native-preexec-agent.service\n"
            )
        run, role = self.run_of_pid(pid)
        if run is not None and name == "status":
            scenario = self.scenario
            # Only the candidate maps to the unprivileged identity; the
            # executor's own supervisor stays root in the container.
            if role == "workload" and not scenario.no_candidate:
                host_id = SUBUID + scenario.candidate_uid - 1
            else:
                host_id = 0
            ids = "\t".join([str(host_id)] * 4)
            capabilities = "".join(
                f"{key}:\t{scenario.capabilities[field]:016x}\n"
                for key, field in (
                    ("CapInh", "inheritable"),
                    ("CapPrm", "permitted"),
                    ("CapEff", "effective"),
                    ("CapBnd", "bounding"),
                    ("CapAmb", "ambient"),
                )
            )
            return (
                f"Uid:\t{ids}\nGid:\t{ids}\n{capabilities}"
                f"NoNewPrivs:\t{scenario.no_new_privs}\n"
                f"Seccomp:\t{scenario.seccomp_mode}\n"
            ).encode()
        return super().proc(pid, name)


class PreexecWorld(resource.ResourceWorld):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.config = {
            **self.config,
            "schema": COLLECTOR.PREEXEC_CONFIG_SCHEMA,
            "preexec_fixtures": str(resource.PROFILE_DIR / "preexec-fixtures.json"),
        }

    def profile_bytes(self) -> dict[str, bytes]:
        return {
            **super().profile_bytes(),
            "preexec-fixtures.json": FIXTURES_RAW,
        }

    def inputs(self) -> dict[str, str]:
        return {
            **super().inputs(),
            "preexec_fixtures_sha256": base.INPUTS["preexec_fixtures_sha256"],
        }

    def host(self, scenario: PreexecScenario | None = None) -> PreexecFakeHost:
        return PreexecFakeHost(self, scenario or PreexecScenario())

    def collector(self, host, kind: str = "preexec"):  # noqa: ARG002
        config = COLLECTOR.parse_preexec_config(json.dumps(self.config).encode())
        return COLLECTOR.PreexecCollector(host, config, checkout=self.world.checkout)

    def collect(self, scenario: PreexecScenario | None = None):
        host = self.host(scenario)
        return self.collector(host).collect(), host


@pytest.fixture
def pw(tmp_path):
    return PreexecWorld(tmp_path)


def observed(record: dict, probe_id: str, language: str = "python") -> dict:
    return base.find_probe(record, probe_id, language)["observed"]


# ---------------------------------------------------------------------------
# Accept path


def test_collected_preexec_record_verifies_offline(pw):
    record, host = pw.collect()
    assert pw.verify(record) is None
    assert record["kind"] == "preexec_confinement"
    assert [phase["name"] for phase in record["phases"]] == [
        "controls",
        "identity",
        "hostile",
    ]
    assert all(
        probe["matched"] for phase in record["phases"] for probe in phase["probes"]
    )
    # Every catalog probe, for every language, exactly once.
    ids = [
        (probe["id"], probe["language"])
        for phase in record["phases"]
        for probe in phase["probes"]
    ]
    assert len(ids) == len(set(ids)) == 22 * 4
    assert host.work_dir_removed


def test_controls_report_one_suite_and_the_hang_times_out(pw):
    record, _ = pw.collect()
    for language in LANGUAGES:
        digests = {
            observed(record, f"control.{name}", language)["suite_sha256"]
            for name in CONTROLS
        }
        assert digests == {FIXTURES["languages"][language]["controls_suite_sha256"]}
        assert observed(record, "control.hang", language)["timed_out"] is True
        assert observed(record, "control.pass", language)["passed"] == 2
        wrong = observed(record, "control.wrong", language)
        assert 1 <= wrong["passed"] < wrong["total"]


def test_identity_comes_from_the_live_candidate(pw):
    record, _ = pw.collect()
    for language in LANGUAGES:
        assert observed(record, "identity.candidate", language) == {
            "uid": CANDIDATE,
            "gid": CANDIDATE,
        }
        assert observed(record, "identity.host_ids", language) == {
            "host_uid": SUBUID + CANDIDATE - 1,
            "host_gid": SUBUID + CANDIDATE - 1,
        }
        assert observed(record, "identity.capabilities", language) == dict.fromkeys(
            CAPABILITY_SETS, 0
        )
        assert observed(record, "identity.no_new_privs", language) == {
            "no_new_privs": 1
        }
        assert observed(record, "identity.seccomp", language) == {"seccomp_mode": 2}


def test_every_hostile_fixture_runs_its_own_subject(pw):
    _, host = pw.collect()
    started = [value for value in host.requests if value["op"] == "start"]
    for language in LANGUAGES:
        entry = FIXTURES["languages"][language]
        wanted = {f"control.{name}" for name in CONTROLS}
        wanted |= {f"hostile.{name}" for name in entry["hostile"]}
        ran = {value["fixture"] for value in started if value["language"] == language}
        assert ran == wanted


# ---------------------------------------------------------------------------
# Refusals: a host that is not confined must not verify


def test_an_unconfined_hostile_subject_does_not_verify(pw):
    scenario = PreexecScenario()
    scenario.unconfined = {("python", "hostile.ptrace")}
    record, _ = pw.collect(scenario)
    assert observed(record, "hostile.ptrace")["outcome"] == "permitted"
    assert base.find_probe(record, "hostile.ptrace", "python")["matched"] is False
    assert pw.verify(record) == "probe hostile.ptrace did not match"


def test_a_present_credential_environment_does_not_verify(pw):
    scenario = PreexecScenario()
    scenario.unconfined = {("go", "hostile.credential_env")}
    record, _ = pw.collect(scenario)
    assert observed(record, "hostile.credential_env", "go")["outcome"] == "present"
    assert pw.verify(record) is not None


def test_a_privileged_candidate_does_not_verify(pw):
    scenario = PreexecScenario()
    scenario.capabilities = {**scenario.capabilities, "effective": 1 << 21}
    record, _ = pw.collect(scenario)
    probe = base.find_probe(record, "identity.capabilities", "python")
    assert probe["matched"] is False
    assert pw.verify(record) is not None


def test_a_candidate_without_no_new_privs_does_not_verify(pw):
    scenario = PreexecScenario()
    scenario.no_new_privs = 0
    record, _ = pw.collect(scenario)
    assert pw.verify(record) is not None


def test_an_unfiltered_candidate_does_not_verify(pw):
    scenario = PreexecScenario()
    scenario.seccomp_mode = 0
    record, _ = pw.collect(scenario)
    assert pw.verify(record) is not None


def test_a_hostile_timeout_is_not_read_as_confinement(pw):
    scenario = PreexecScenario()
    scenario.timed_out = {("node", "hostile.mount")}
    record, _ = pw.collect(scenario)
    assert observed(record, "hostile.mount", "node")["outcome"] == "probe_error"
    assert pw.verify(record) is not None


# ---------------------------------------------------------------------------
# Refusals: the collector refuses before it can write a misleading record


def refusal(pw, scenario) -> str:
    with pytest.raises(COLLECTOR.Refusal) as error:
        pw.collect(scenario)
    return str(error.value)


def test_a_build_that_never_ran_the_suite_is_refused(pw):
    scenario = PreexecScenario()
    scenario.build_failed = {("python", "hostile.setuid")}
    assert "never reached its suite" in refusal(pw, scenario)


def test_a_staged_subject_other_than_the_pinned_one_is_refused(pw):
    scenario = PreexecScenario()
    scenario.subject_digest = base.digest("another subject")
    assert "staged other bytes" in refusal(pw, scenario)


def test_a_staged_suite_other_than_the_pinned_one_is_refused(pw):
    scenario = PreexecScenario()
    scenario.suite_digest = base.digest("another suite")
    assert "staged other bytes" in refusal(pw, scenario)


def test_a_candidate_of_another_identity_is_refused(pw):
    # The approved identity is how the collector tells the candidate from the
    # executor's root supervisor. A container where no process holds it yields
    # no identity observation at all, so collection refuses rather than
    # guessing which process the fixture compiled.
    scenario = PreexecScenario()
    scenario.candidate_uid = CANDIDATE + 1
    assert "candidate process did not start" in refusal(pw, scenario)


def test_a_container_without_a_candidate_is_refused(pw):
    scenario = PreexecScenario()
    scenario.no_candidate = True
    assert "candidate process did not start" in refusal(pw, scenario)


def test_an_agent_reading_other_documents_is_refused(pw):
    scenario = PreexecScenario()
    scenario.agent_inputs = {"grading_profile_sha256": base.digest("other")}
    assert "read other profile documents" in refusal(pw, scenario)


def test_a_control_suite_of_one_test_is_refused_by_the_verifier(pw):
    scenario = PreexecScenario()
    scenario.control_total = 1
    scenario.wrong_passed = 0
    record, _ = pw.collect(scenario)
    assert pw.verify(record) is not None
