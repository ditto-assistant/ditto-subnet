"""The Go probe runner never produces evidence it did not measure.

In PR2 the runner writes no evidence record. Its only output is a requested
configuration observation report, which the offline verifier must refuse as a
record. When the rootless CI job provides a live report, it is compared with
limits the offline verifier derives from the committed approved grading
profile, not with values taken from the report. No host, custody path or
credential is touched.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
API = ROOT / "services/dittobench-api"
CMD = "./cmd/dittobench-coding-enforcement-probe"
CI_PROFILE = API / "internal/codingenforcement/probe/testdata/ci-grading-profile.json"
REPORT_SCHEMA = "dittobench-coding-native-probe-observations-v1"
# CI sets this so a missing toolchain or failed build fails instead of skipping.
REQUIRE_RUNNER = "DITTOBENCH_REQUIRE_PROBE_RUNNER"
LIVE_REPORT = "DITTOBENCH_NATIVE_PROBE_REPORT"
# Only the rootless probe job sets this; without it a missing report skips.
REQUIRE_LIVE = "DITTOBENCH_REQUIRE_LIVE_PROBE_REPORT"

_spec = importlib.util.spec_from_file_location(
    "native_evidence_for_runner_tests", ROOT / "infra/scripts/coding-native-evidence.py"
)
assert _spec is not None and _spec.loader is not None
EVIDENCE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EVIDENCE)

PR1_TESTS = Path(__file__).parent / "test_coding_native_enforcement_evidence.py"
_pr1_spec = importlib.util.spec_from_file_location("pr1_evidence_tests", PR1_TESTS)
assert _pr1_spec is not None and _pr1_spec.loader is not None
PR1 = importlib.util.module_from_spec(_pr1_spec)
_pr1_spec.loader.exec_module(PR1)


def _unavailable(reason: str):
    if os.environ.get(REQUIRE_RUNNER) == "1":
        pytest.fail(f"{REQUIRE_RUNNER}=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def runner_binary(tmp_path_factory) -> Path:
    go = shutil.which("go")
    if go is None:
        _unavailable("go toolchain is unavailable")
    out = tmp_path_factory.mktemp("probe-runner") / "enforcement-probe"
    build = subprocess.run(
        [str(go), "build", "-o", str(out), CMD],
        cwd=API,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        _unavailable(f"probe runner did not build: {build.stderr}")
    return out


@pytest.mark.parametrize("subcommand", ["assemble", "collect", "record"])
def test_runner_has_no_record_writing_subcommand(runner_binary, subcommand):
    result = subprocess.run(
        [str(runner_binary), subcommand], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "unknown subcommand" in result.stderr


def _report(entries: list[dict]) -> dict:
    return {"schema": REPORT_SCHEMA, "enforcement_measured": False, "entries": entries}


def test_observation_report_is_refused_as_an_evidence_record(tmp_path):
    raw = EVIDENCE.canonical_bytes(_report([]))
    with pytest.raises(EVIDENCE.Refusal):
        EVIDENCE.parse_record_envelope(raw)
    world = PR1.World(tmp_path)
    result, ok = world.verify([world.put(raw)])
    assert not ok and result["records"][0]["failure"] is not None


def test_runner_refuses_a_missing_approved_image_instead_of_pulling(runner_binary):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    if subprocess.run([docker, "info"], capture_output=True, check=False).returncode:
        pytest.skip("docker daemon is unavailable")
    absent = "registry.invalid/enforcement-probe-e2e@sha256:" + "0" * 64
    result = subprocess.run(
        [str(runner_binary), "resolve-images", absent],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "refusing to pull" in result.stderr, result.stderr


def _expected_requested_config(language: str) -> dict:
    """Limits derived independently by the offline verifier's profile parser."""

    policy = EVIDENCE.parse_grading_profile(CI_PROFILE.read_bytes())["policy"]
    scratch = policy["ScratchLimitBytes"]
    if language == "rust":
        scratch -= min(scratch // 2, 128 << 20)
    return {
        "memory_limit_bytes": policy["MemoryLimitBytes"],
        "memory_swap_bytes": 0,
        "cpu_quota_millis": policy["CPUQuotaMillis"],
        "pids_limit": policy["PidsLimit"],
        "scratch_limit_bytes": scratch,
        "read_only_rootfs": True,
    }


def _live_report() -> dict:
    path = os.environ.get(LIVE_REPORT)
    if not path:
        if os.environ.get(REQUIRE_LIVE) == "1":
            pytest.fail(f"{REQUIRE_LIVE}=1 but {LIVE_REPORT} is unset")
        pytest.skip(f"{LIVE_REPORT} is unset; the rootless CI job provides it")
    return json.loads(Path(str(path)).read_text())


def test_live_requested_config_equals_the_approved_profile():
    report = _live_report()
    assert report["schema"] == REPORT_SCHEMA
    assert report["enforcement_measured"] is False
    assert report["entries"]
    for entry in report["entries"]:
        assert entry["container_class"] == "executor_grading"
        assert entry["source"] == "docker_inspect_created_unstarted_container"
        assert entry["requested_config"] == _expected_requested_config(
            entry["language"]
        ), entry


def test_live_report_is_refused_as_an_evidence_record():
    raw = EVIDENCE.canonical_bytes(_live_report())
    with pytest.raises(EVIDENCE.Refusal):
        EVIDENCE.parse_record_envelope(raw)
