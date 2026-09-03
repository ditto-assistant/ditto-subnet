from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hippius_canary import (
    HIPPIUS_SHADOW_CANARY_CONFIRMATION,
    HippiusShadowCanaryAuthoringMaterial,
    HippiusShadowCanaryGradingMaterial,
)
from ditto.api_server.coding_hippius_canary_operator import (
    HippiusCanaryOperatorError,
    ProcessHippiusCanaryAuthoringExecutor,
    ProcessHippiusCanaryGradingExecutor,
    ProcessHippiusPrivateInputUnwrapper,
    ProtectedCanonicalHelper,
    _run_helper_process,
    load_hippius_shadow_canary_plan,
    parse_hippius_canary_operator_config,
    resolve_clean_repository_source_sha,
)
from ditto.api_server.coding_hippius_retrieval import (
    HippiusPrivateInputUnwrapRequest,
)
from ditto.tests.api_server.test_coding_hippius_canary import (
    _SOURCE,
    _plan,
    _synthetic_record,
)

ROOT = Path(__file__).parents[5]
_PLAN_SCHEMA = "dittobench-coding-hippius-shadow-canary-plan-v1"


def _operator_script() -> ModuleType:
    path = ROOT / "apps/platform/scripts/run_hippius_shadow_canary.py"
    spec = importlib.util.spec_from_file_location("run_hippius_shadow_canary", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _protected_directory(path: Path) -> Path:
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path.resolve()


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o500)
    return path.resolve()


def _helper_program(mode: str = "valid") -> str:
    return f"""#!{sys.executable}
import base64
import json
import os
import sys
import time

request = json.loads(sys.stdin.buffer.read())
if any(name.startswith('DITTO_CODING_HIPPIUS_') for name in os.environ):
    raise SystemExit(8)
mode = {mode!r}
if mode == 'timeout':
    time.sleep(5)
if mode == 'noncanonical':
    sys.stdout.write('{{}}')
    raise SystemExit(0)
schema = request['schema']
if schema.endswith('unwrap-helper-request-v1'):
    response = {{
        'data_key_b64': base64.b64encode(b'x' * 32).decode(),
        'expires_at': request['ticket_deadline'],
        'request_sha256': request['request_sha256'],
        'schema': 'dittobench-coding-hippius-canary-unwrap-helper-response-v1',
        'weight_eligible': False,
    }}
elif schema.endswith('authoring-helper-request-v1'):
    if 'grader_plan' in request or request['phase'] != 'authoring':
        raise SystemExit(9)
    response = {{
        'execution_authority_sha256': request['execution_authority_sha256'],
        'frozen_submission_b64': base64.b64encode(b'patch').decode(),
        'resolved': True,
        'schema': 'dittobench-coding-hippius-canary-authoring-helper-response-v1',
        'task_commitment_sha256': request['task_commitment_sha256'],
        'transcript_b64': base64.b64encode(b'transcript').decode(),
        'weight_eligible': False,
    }}
elif schema.endswith('grading-helper-request-v1'):
    if 'issue' in request or request['phase'] != 'grading':
        raise SystemExit(10)
    if base64.b64decode(request['frozen_submission_b64']) != b'patch':
        raise SystemExit(11)
    response = {{
        'execution_authority_sha256': request['execution_authority_sha256'],
        'frozen_submission_sha256': request['frozen_submission_sha256'],
        'pristine': True,
        'resolved': True,
        'schema': 'dittobench-coding-hippius-canary-grading-helper-response-v1',
        'task_commitment_sha256': request['task_commitment_sha256'],
        'terminal_evidence_b64': base64.b64encode(b'terminal').decode(),
        'weight_eligible': False,
    }}
else:
    raise SystemExit(12)
sys.stdout.write(json.dumps(response, sort_keys=True, separators=(',', ':')) + '\\n')
"""


def _helper(tmp_path: Path, *, mode: str = "valid") -> ProtectedCanonicalHelper:
    root = _protected_directory(tmp_path / f"work-{mode}")
    executable = _write_executable(tmp_path / f"helper-{mode}", _helper_program(mode))
    return ProtectedCanonicalHelper(
        executable=executable,
        work_root=root,
        timeout_seconds=1,
    )


def _plan_projection() -> dict[str, object]:
    commitment, record = _synthetic_record()
    plan = _plan(commitment, record)
    private = asdict(plan.private_input)
    private["commitment"] = plan.private_input.commitment.model_dump(
        mode="json", by_alias=True
    )
    private["ticket_id"] = str(plan.private_input.ticket_id)
    private["run_row_id"] = str(plan.private_input.run_row_id)
    private["ticket_deadline"] = plan.private_input.ticket_deadline.isoformat().replace(
        "+00:00", "Z"
    )
    private["delivery_phase"] = plan.private_input.delivery_phase.value
    sealed = asdict(plan.sealed_evidence)
    sealed["ticket_id"] = str(plan.sealed_evidence.ticket_id)
    sealed["ticket_deadline"] = (
        plan.sealed_evidence.ticket_deadline.isoformat().replace("+00:00", "Z")
    )
    sealed["evidence_kind"] = plan.sealed_evidence.evidence_kind.value
    return {
        "canary_id": str(plan.canary_id),
        "private_input": private,
        "schema": _PLAN_SCHEMA,
        "sealed_evidence": sealed,
        "single_validator": True,
        "source_sha": plan.source_sha,
        "synthetic_corpus_release_id": plan.synthetic_corpus_release_id,
        "synthetic_only": True,
        "synthetic_record_sha256": plan.synthetic_record_sha256,
        "weight_eligible": False,
    }


def _write_plan(path: Path, projection: dict[str, object] | None = None) -> Path:
    path.write_bytes(
        coding_canonical_json_bytes(
            _plan_projection() if projection is None else projection,
            maximum_bytes=256 << 10,
            label="test Hippius canary plan",
        )
    )
    path.chmod(0o600)
    return path.resolve()


def test_operator_config_is_default_off_and_requires_distinct_absolute_helpers(
    tmp_path: Path,
) -> None:
    assert parse_hippius_canary_operator_config({}) is None
    names = {
        "DITTO_CODING_HIPPIUS_CANARY_ENABLED": "true",
        "DITTO_CODING_HIPPIUS_CANARY_PLAN_PATH": str(tmp_path / "plan"),
        "DITTO_CODING_HIPPIUS_CANARY_DEPLOYED_SOURCE_PATH": str(tmp_path / "source"),
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_MANIFEST_PATH": str(tmp_path / "manifest"),
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_PUBLICATION_RECEIPT_PATH": str(
            tmp_path / "publication"
        ),
        "DITTO_CODING_HIPPIUS_CURATOR_PUBLIC_KEY_PATH": str(tmp_path / "curator"),
        "DITTO_CODING_HIPPIUS_CANARY_UNWRAP_EXECUTABLE": str(tmp_path / "unwrap"),
        "DITTO_CODING_HIPPIUS_CANARY_AUTHORING_EXECUTABLE": str(tmp_path / "author"),
        "DITTO_CODING_HIPPIUS_CANARY_GRADING_EXECUTABLE": str(tmp_path / "grade"),
        "DITTO_CODING_HIPPIUS_CANARY_HELPER_WORK_ROOT": str(tmp_path / "work"),
        "DITTO_CODING_HIPPIUS_CANARY_HELPER_TIMEOUT_SECONDS": "30",
    }
    config = parse_hippius_canary_operator_config(names)
    assert config is not None
    assert config.helper_timeout_seconds == 30
    assert "plan" not in repr(config)
    names["DITTO_CODING_HIPPIUS_CANARY_GRADING_EXECUTABLE"] = names[
        "DITTO_CODING_HIPPIUS_CANARY_AUTHORING_EXECUTABLE"
    ]
    with pytest.raises(HippiusCanaryOperatorError, match="unsafe"):
        parse_hippius_canary_operator_config(names)
    names["DITTO_CODING_HIPPIUS_CANARY_GRADING_EXECUTABLE"] = "relative"
    with pytest.raises(HippiusCanaryOperatorError, match="relative"):
        parse_hippius_canary_operator_config(names)


def test_plan_loader_requires_canonical_owner_only_synthetic_plan(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path / "plan.json")
    plan = load_hippius_shadow_canary_plan(plan_path)
    assert plan.source_sha == _SOURCE
    assert plan.synthetic_only is True
    assert plan.single_validator is True
    assert plan.weight_eligible is False
    assert plan.private_input.ticket_id == plan.sealed_evidence.ticket_id

    plan_path.chmod(0o644)
    with pytest.raises(HippiusCanaryOperatorError, match="unsafe"):
        load_hippius_shadow_canary_plan(plan_path)
    plan_path.chmod(0o600)
    raw = _plan_projection()
    raw["synthetic_only"] = False
    forged = _write_plan(tmp_path / "forged.json", raw)
    with pytest.raises(HippiusCanaryOperatorError, match="invalid"):
        load_hippius_shadow_canary_plan(forged)


async def test_protected_helpers_keep_phases_separate_and_unwrap_external(
    tmp_path: Path,
) -> None:
    helper = _helper(tmp_path)
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    unwrap_request = HippiusPrivateInputUnwrapRequest(
        ticket_id=_plan(*_synthetic_record()).private_input.ticket_id,
        run_row_id=_plan(*_synthetic_record()).private_input.run_row_id,
        validator_hotkey=_plan(*_synthetic_record()).private_input.validator_hotkey,
        coding_run_id="hippius-canary-run-001",
        assignment_sha256="1" * 64,
        run_manifest_sha256="2" * 64,
        ticket_deadline=deadline,
        delivery_phase=_plan(*_synthetic_record()).private_input.delivery_phase,
        catalog_commitment_sha256="3" * 64,
        catalog_index=0,
        transport_manifest_sha256="4" * 64,
        publication_receipt_payload_sha256="5" * 64,
        wrapping_key_sha256="6" * 64,
        aad_sha256="7" * 64,
        ciphertext_sha256="8" * 64,
        wrapped_data_key=b"wrapped",
        request_sha256="9" * 64,
    )
    unwrapped = await ProcessHippiusPrivateInputUnwrapper(helper).unwrap_data_key(
        unwrap_request
    )
    assert unwrapped.data_key == b"x" * 32
    assert unwrapped.request_sha256 == unwrap_request.request_sha256
    assert "data_key" not in repr(unwrapped)

    _commitment, record = _synthetic_record()
    task_sha256 = record.task_version.task_commitment_sha256
    authoring = await ProcessHippiusCanaryAuthoringExecutor(helper).execute_authoring(
        material=HippiusShadowCanaryAuthoringMaterial(
            execution_authority_sha256="a" * 64,
            task_commitment_sha256=task_sha256,
            ticket_deadline=deadline,
            issue=record.issue,
            runtime_policy=record.runtime_policy,
            budgets=record.budgets,
            runner_plan=record.runner_plan,
        )
    )
    assert authoring.transcript == b"transcript"
    assert authoring.frozen_submission == b"patch"
    grading = await ProcessHippiusCanaryGradingExecutor(helper).execute_grading(
        material=HippiusShadowCanaryGradingMaterial(
            execution_authority_sha256="a" * 64,
            task_commitment_sha256=task_sha256,
            ticket_deadline=deadline,
            frozen_submission=authoring.frozen_submission,
            frozen_submission_sha256=hashlib.sha256(
                authoring.frozen_submission
            ).hexdigest(),
            grader_plan=record.grader_plan,
            resource_profile=record.grader_resource_profile,
        )
    )
    assert grading.terminal_evidence == b"terminal"
    assert grading.pristine is True
    assert "terminal" not in repr(grading)


async def test_helper_rejects_noncanonical_output_and_timeout(tmp_path: Path) -> None:
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    for mode, message in (("noncanonical", "invalid"), ("timeout", "timed out")):
        helper = _helper(tmp_path / mode, mode=mode)
        with pytest.raises(HippiusCanaryOperatorError, match=message):
            await helper.call(
                projection={"schema": "test"},
                deadline=deadline,
            )


def test_helper_paths_and_repository_source_fail_closed(tmp_path: Path) -> None:
    root = _protected_directory(tmp_path / "work")
    executable = _write_executable(tmp_path / "helper", _helper_program())
    executable.chmod(0o755)
    with pytest.raises(HippiusCanaryOperatorError, match="unsafe"):
        ProtectedCanonicalHelper(
            executable=executable,
            work_root=root,
            timeout_seconds=30,
        )
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "canary@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Canary"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("exact\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "exact"], cwd=repository, check=True)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert resolve_clean_repository_source_sha(repository.resolve()) == expected
    (repository / "tracked.txt").write_text("dirty\n")
    with pytest.raises(HippiusCanaryOperatorError, match="clean revision"):
        resolve_clean_repository_source_sha(repository.resolve())


def test_operator_script_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _operator_script()
    with pytest.raises(SystemExit) as error:
        script.main(["--confirm", "RUN", "--output", "/tmp/receipt"])
    assert error.value.code == 2

    async def fake_run(**_kwargs: object) -> tuple[str, str]:
        return "a" * 64, "b" * 64

    monkeypatch.setattr(script, "run_hippius_canary_operator_from_env", fake_run)
    assert (
        script.main(
            [
                "--confirm",
                HIPPIUS_SHADOW_CANARY_CONFIRMATION,
                "--output",
                "/tmp/new-receipt",
            ]
        )
        == 0
    )


@pytest.mark.asyncio
async def test_helper_timeout_kills_forked_process_group(tmp_path: Path) -> None:
    work_root = _protected_directory(tmp_path / "work")
    child_pid_path = work_root / "child.pid"
    executable = _write_executable(
        tmp_path / "forking-helper",
        f"""#!{sys.executable}
import os
import time

child = os.fork()
if child == 0:
    time.sleep(30)
    os._exit(0)
with open({str(child_pid_path)!r}, "w", encoding="ascii") as handle:
    handle.write(str(child))
time.sleep(30)
""",
    )
    with pytest.raises(HippiusCanaryOperatorError, match="timed out"):
        await _run_helper_process(
            executable=executable,
            work_root=work_root,
            body=b"{}",
            timeout_seconds=1.0,
        )
    deadline = time.monotonic() + 2
    child_pid = None
    while time.monotonic() < deadline:
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text())
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("forked helper child survived operator timeout")
    if child_pid is not None:
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
