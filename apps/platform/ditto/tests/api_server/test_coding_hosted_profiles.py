"""Synthetic producer integration for the hosted-v2 profile operator helper."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_control import SourceBinding
from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hosted_grading import expected_grading
from ditto.api_server.coding_private_catalog_v2_compile import (
    compile_private_catalog_v2,
)
from ditto.api_server.coding_private_v2_payload import build_private_v2_payload
from ditto.tests.api_server.test_coding_private_v2_payload import _bound_fixture

REPO = Path(__file__).resolve().parents[5]
INDEX = 7
LIMITS = {
    "MaxBundleBytes": 16 << 20,
    "MaxWorkspaceBytes": 64 << 20,
    "MaxFileBytes": 4 << 20,
    "MaxPatchBytes": 1 << 20,
    "MaxEntries": 50000,
    "MaxToolCalls": 128,
    "MaxReadBytes": 32 << 10,
    "MaxResponseBytes": 256 << 10,
    "MaxSearchResults": 100,
    "MaxReplayCacheBytes": 64 << 20,
    "MaxTranscriptBytes": 128 << 20,
}


@pytest.fixture(scope="session")
def profiles_binary(tmp_path_factory):
    root = tmp_path_factory.mktemp("hosted-profiles")
    root.chmod(0o700)
    path = root / "dittobench-coding-hosted-profiles"
    subprocess.run(
        ["go", "build", "-o", str(path), "./cmd/dittobench-coding-hosted-profiles"],
        cwd=REPO / "services/dittobench-api",
        check=True,
        capture_output=True,
        timeout=300,
    )
    return path


@pytest.fixture(scope="module")
def payload(tmp_path_factory):
    root = tmp_path_factory.mktemp("hosted-profile-payload")
    root.chmod(0o700)
    release_path, groups = _bound_fixture(root, native_authoring=True)
    compile_private_catalog_v2(
        release_authority=release_path, groups_root=groups, output=root / "catalog"
    )
    authority = build_private_v2_payload(
        catalog_directory=root / "catalog", groups_root=groups, output=root / "payload"
    )
    return root / "payload", authority


def driver(group: str, suite: str) -> dict:
    return {
        "group": group,
        "command": {
            "id": f"{group}-tests",
            "argv": [
                "dittobench-test-driver",
                "--group",
                group,
                "--suite",
                suite,
                "--module",
                "app",
                "--candidate-timeout-ms",
                "1000",
            ],
            "timeout_milliseconds": 60000,
        },
        "expected_total": 1,
    }


def request() -> dict:
    return {
        "schema": "dittobench-coding-hosted-profile-request-v1",
        "catalog_index": INDEX,
        "image_digest": "sha256:" + "9" * 64,
        "candidate_limits": dict(LIMITS),
        "protected_limits": dict(LIMITS),
        "max_combined_disk_bytes": 1 << 30,
        "budgets": {
            "model_input_tokens": 200000,
            "model_output_tokens": 32000,
            "workspace_tool_calls": 128,
            "wall_time_seconds": 600,
        },
        "build": {
            "required": False,
            "command": {
                "id": "python-compile",
                "argv": ["python", "-m", "compileall", "app.py"],
                "timeout_milliseconds": 30000,
            },
        },
        "test_groups": [driver("hidden", "test_app.py"), driver("visible", "app.py")],
        "execution_timeout_milliseconds": 600000,
        "shadow_only": True,
        "weight_eligible": False,
    }


def run(binary: Path, tmp_path: Path, payload_dir: Path, body: dict, name="out"):
    tmp_path.chmod(0o700)
    request_path = tmp_path / f"{name}-request.json"
    request_path.write_text(json.dumps(body))
    output = tmp_path / name
    result = subprocess.run(
        [
            str(binary),
            "--request",
            str(request_path),
            "--payload-authority",
            str(payload_dir / "payload-authority.json"),
            "--objects",
            str(payload_dir / "objects"),
            "--output",
            str(output),
        ],
        capture_output=True,
        timeout=300,
        env={"PATH": os.environ["PATH"]},
    )
    return result, output


def test_profiles_pass_launch_checks_and_platform_canonical_loaders(
    profiles_binary, payload, tmp_path
) -> None:
    payload_dir, authority = payload
    result, output = run(profiles_binary, tmp_path, payload_dir, request())
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == json.loads((output / "receipt.json").read_bytes())
    task = next(t for t in authority["task_assets"] if t["catalog_index"] == INDEX)
    execution = (output / "execution-profile.json").read_bytes()
    grading = (output / "grading-profile.json").read_bytes()
    for body, limit in ((execution, 16384), (grading, 65536)):
        value = _decode_json_document(body, maximum_bytes=limit)
        assert (
            coding_canonical_json_bytes(value, maximum_bytes=limit, label="profile")
            == body
        )
    assert hashlib.sha256(execution).hexdigest() == receipt["execution_profile_sha256"]
    assert hashlib.sha256(grading).hexdigest() == receipt["grading_profile_sha256"]
    assert receipt["catalog_index"] == INDEX
    assert receipt["task_commitment_sha256"] == task["task_commitment_sha256"]
    assert receipt["grader_bundle_sha256"] == task["artifacts"]["grader_bundle"]
    # Hosted v2 binds no test manifest, and never reuses the grader bundle digest.
    assert "test_manifest_sha256" not in receipt
    assert b"test_manifest" not in grading
    assert receipt["max_patch_bytes"] == LIMITS["MaxPatchBytes"]
    assert receipt["launch_checks_passed"] is True
    assert receipt["approved"] is False
    assert receipt["shadow_only"] is True and receipt["weight_eligible"] is False
    policy = json.loads(execution)["resource_policy"]
    # Task-bound sandbox values come from the private resource profile, not the request.
    assert (policy["CPUQuotaMillis"], policy["PidsLimit"]) == (2000, 256)
    assert policy["MemoryLimitBytes"] == 1024 << 20
    assert policy["ScratchLimitBytes"] == 512 << 20
    for mode in (output.stat().st_mode, *(p.stat().st_mode for p in output.iterdir())):
        assert mode & 0o077 == 0
    source = SourceBinding(
        evaluation_id=uuid4(),
        attempt_id=uuid4(),
        worker_id=uuid4(),
        assignment_sha256="1" * 64,
        artifact_sha256="2" * 64,
        harness_instance_id="profile-test",
        profile_capability_id="hosted-profile-test",
        deadline_unix=2_000_000_000,
    )
    plan = expected_grading(
        json.loads(grading),
        source,
        {
            "visible_bundle_sha256": "3" * 64,
            "base_tree_sha256": "4" * 64,
            "final_tree_sha256": "5" * 64,
        },
    )
    assert [g["total"] for g in plan["groups"]] == [1, 1]


def _set(path: list, value):
    def mutate(body: dict) -> None:
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_set(["catalog_index"], 250), id="unknown-index"),
        pytest.param(_set(["budgets", "wall_time_seconds"], 601), id="wall-over-task"),
        pytest.param(
            _set(["budgets", "workspace_tool_calls"], 127), id="tool-call-drift"
        ),
        pytest.param(
            _set(["candidate_limits", "MaxBundleBytes"], 512), id="bundle-too-small"
        ),
        pytest.param(_set(["build", "required"], True), id="build-not-declared"),
        pytest.param(
            _set(["test_groups", 0, "command", "argv", 4], "missing.py"),
            id="hidden-suite-absent",
        ),
        pytest.param(
            _set(["test_groups", 1, "command", "argv", 4], "test_app.py"),
            id="visible-suite-only-in-grader",
        ),
        pytest.param(
            _set(["test_groups", 0, "command", "argv", 2], "visible"),
            id="group-flag-mismatch",
        ),
        pytest.param(
            _set(["test_groups", 0, "command", "argv", 4], "../test_app.py"),
            id="suite-escape",
        ),
        pytest.param(_set(["test_groups", 0, "expected_total"], 0), id="zero-count"),
        pytest.param(_set(["image_digest"], "sha256:abc"), id="image-digest"),
        pytest.param(_set(["weight_eligible"], True), id="weight-eligible"),
        pytest.param(_set(["extra"], 1), id="unknown-field"),
        pytest.param(lambda body: body["test_groups"].reverse(), id="group-order"),
    ],
)
def test_rejected_requests_write_nothing(
    profiles_binary, payload, tmp_path, mutate
) -> None:
    body = copy.deepcopy(request())
    mutate(body)
    result, output = run(profiles_binary, tmp_path, payload[0], body)
    assert result.returncode == 70
    assert result.stdout == b""
    assert result.stderr == b"hosted profile request rejected\n"
    assert not output.exists()


def test_tampered_object_and_existing_output_are_rejected(
    profiles_binary, payload, tmp_path
) -> None:
    payload_dir, authority = payload
    first, _ = run(profiles_binary, tmp_path, payload_dir, request(), name="again")
    assert first.returncode == 0
    second, _ = run(profiles_binary, tmp_path, payload_dir, request(), name="again")
    assert second.returncode == 70
    tampered = tmp_path / "tampered"
    tampered.mkdir(mode=0o700)
    (tampered / "objects").mkdir(mode=0o700)
    (tampered / "payload-authority.json").write_bytes(
        (payload_dir / "payload-authority.json").read_bytes()
    )
    task = next(t for t in authority["task_assets"] if t["catalog_index"] == INDEX)
    for source in (payload_dir / "objects").iterdir():
        body = source.read_bytes()
        if source.name == task["artifacts"]["grader_bundle"] + ".bin":
            body = body[:-1] + bytes([body[-1] ^ 1])
        (tampered / "objects" / source.name).write_bytes(body)
    result, output = run(profiles_binary, tmp_path, tampered, request(), name="t")
    assert result.returncode == 70 and not output.exists()
