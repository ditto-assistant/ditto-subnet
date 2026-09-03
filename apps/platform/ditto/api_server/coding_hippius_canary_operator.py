"""Default-off one-shot operator wiring for the Hippius Coding canary."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_evidence import CodingSealedEvidenceKind
from ditto.api_server.coding_hippius_canary import (
    HIPPIUS_SHADOW_CANARY_CONFIRMATION,
    HippiusShadowCanaryAuthoringMaterial,
    HippiusShadowCanaryAuthoringOutcome,
    HippiusShadowCanaryError,
    HippiusShadowCanaryGradingMaterial,
    HippiusShadowCanaryGradingOutcome,
    HippiusShadowCanaryPlan,
    run_hippius_shadow_canary,
    write_hippius_shadow_canary_receipt,
)
from ditto.api_server.coding_hippius_custody import (
    HippiusEvidenceRuntime,
    create_hippius_evidence_runtime_from_env,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceSourceAuthority,
)
from ditto.api_server.coding_hippius_retrieval import (
    AiobotoHippiusPrivateInputReader,
    HippiusPrivateInputRetriever,
    HippiusPrivateInputTicketAuthority,
    HippiusPrivateInputUnwrapRequest,
    HippiusPrivateInputUnwrapResult,
    parse_hippius_private_input_retrieval_config,
)
from ditto.db import create_db_engine, create_session_maker

HIPPIUS_CANARY_OPERATOR_ENABLED = "DITTO_CODING_HIPPIUS_CANARY_ENABLED"

_PLAN_SCHEMA = "dittobench-coding-hippius-shadow-canary-plan-v1"
_UNWRAP_REQUEST_SCHEMA = "dittobench-coding-hippius-canary-unwrap-helper-request-v1"
_UNWRAP_RESPONSE_SCHEMA = "dittobench-coding-hippius-canary-unwrap-helper-response-v1"
_AUTHORING_REQUEST_SCHEMA = (
    "dittobench-coding-hippius-canary-authoring-helper-request-v1"
)
_AUTHORING_RESPONSE_SCHEMA = (
    "dittobench-coding-hippius-canary-authoring-helper-response-v1"
)
_GRADING_REQUEST_SCHEMA = "dittobench-coding-hippius-canary-grading-helper-request-v1"
_GRADING_RESPONSE_SCHEMA = "dittobench-coding-hippius-canary-grading-helper-response-v1"
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_PLAN_BYTES = 256 << 10
_MAX_HELPER_REQUEST_BYTES = 16 << 20
_MAX_HELPER_RESPONSE_BYTES = 24 << 20
_MAX_HELPER_SECONDS = 2 * 60 * 60
_MIN_HELPER_SECONDS = 1
_HELPER_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}


class HippiusCanaryOperatorError(RuntimeError):
    """Operator configuration or execution failed without sensitive details."""


@dataclass(frozen=True, repr=False)
class HippiusCanaryOperatorConfig:
    plan_path: Path
    deployed_source_path: Path
    private_input_manifest_path: Path
    private_input_publication_receipt_path: Path
    curator_public_key_path: Path
    unwrap_executable: Path
    authoring_executable: Path
    grading_executable: Path
    helper_work_root: Path
    helper_timeout_seconds: int

    def __repr__(self) -> str:
        return "HippiusCanaryOperatorConfig(enabled=True, one_shot=True)"


class ProtectedCanonicalHelper:
    """One fixed owner-only executable with bounded canonical stdio."""

    def __init__(
        self,
        *,
        executable: Path,
        work_root: Path,
        timeout_seconds: int,
    ) -> None:
        _validate_protected_executable(executable)
        _validate_owner_directory(work_root, label="canary helper work root")
        if not _MIN_HELPER_SECONDS <= timeout_seconds <= _MAX_HELPER_SECONDS:
            raise HippiusCanaryOperatorError("canary helper timeout is invalid")
        self._executable = executable
        self._work_root = work_root
        self._timeout_seconds = timeout_seconds

    async def call(
        self,
        *,
        projection: dict[str, Any],
        deadline: datetime,
    ) -> dict[str, Any]:
        try:
            body = coding_canonical_json_bytes(
                projection,
                maximum_bytes=_MAX_HELPER_REQUEST_BYTES,
                label="Hippius canary helper request",
            )
        except (TypeError, ValueError) as error:
            raise HippiusCanaryOperatorError(
                "canary helper request is invalid"
            ) from error
        now = datetime.now(UTC)
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise HippiusCanaryOperatorError("canary helper deadline is invalid")
        remaining = (deadline.astimezone(UTC) - now).total_seconds()
        timeout_seconds = min(float(self._timeout_seconds), remaining)
        if timeout_seconds <= 0:
            raise HippiusCanaryOperatorError("canary helper deadline expired")
        response = await _run_helper_process(
            executable=self._executable,
            work_root=self._work_root,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        try:
            parsed = json.loads(response, object_pairs_hook=_unique_object)
            if not isinstance(parsed, dict):
                raise ValueError("helper response root is invalid")
            if (
                coding_canonical_json_bytes(
                    parsed,
                    maximum_bytes=_MAX_HELPER_RESPONSE_BYTES,
                    label="Hippius canary helper response",
                )
                != response
            ):
                raise ValueError("helper response is not canonical")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise HippiusCanaryOperatorError(
                "canary helper response is invalid"
            ) from error
        return parsed

    def __repr__(self) -> str:
        return "ProtectedCanonicalHelper(configured=True)"


class ProcessHippiusPrivateInputUnwrapper:
    """Delegate unwrap to an owner-only helper; Platform holds no private key."""

    def __init__(self, helper: ProtectedCanonicalHelper) -> None:
        self._helper = helper

    async def unwrap_data_key(
        self,
        request: HippiusPrivateInputUnwrapRequest,
    ) -> HippiusPrivateInputUnwrapResult:
        projection = {
            "aad_sha256": request.aad_sha256,
            "assignment_sha256": request.assignment_sha256,
            "catalog_commitment_sha256": request.catalog_commitment_sha256,
            "catalog_index": request.catalog_index,
            "ciphertext_sha256": request.ciphertext_sha256,
            "coding_run_id": request.coding_run_id,
            "delivery_phase": request.delivery_phase.value,
            "publication_receipt_payload_sha256": (
                request.publication_receipt_payload_sha256
            ),
            "request_sha256": request.request_sha256,
            "run_manifest_sha256": request.run_manifest_sha256,
            "run_row_id": str(request.run_row_id),
            "schema": _UNWRAP_REQUEST_SCHEMA,
            "ticket_deadline": _utc_text(request.ticket_deadline),
            "ticket_id": str(request.ticket_id),
            "transport_manifest_sha256": request.transport_manifest_sha256,
            "validator_hotkey": request.validator_hotkey,
            "wrapped_data_key_b64": base64.b64encode(request.wrapped_data_key).decode(
                "ascii"
            ),
            "wrapping_key_sha256": request.wrapping_key_sha256,
            "weight_eligible": False,
        }
        raw = await self._helper.call(
            projection=projection,
            deadline=request.ticket_deadline,
        )
        try:
            if set(raw) != {
                "data_key_b64",
                "expires_at",
                "request_sha256",
                "schema",
                "weight_eligible",
            }:
                raise ValueError("unwrap response fields are invalid")
            if raw.pop("schema") != _UNWRAP_RESPONSE_SCHEMA:
                raise ValueError("unwrap response schema is invalid")
            if raw.pop("weight_eligible") is not False:
                raise ValueError("unwrap response eligibility is invalid")
            result = HippiusPrivateInputUnwrapResult(
                request_sha256=str(raw.pop("request_sha256")),
                data_key=base64.b64decode(
                    str(raw.pop("data_key_b64")),
                    validate=True,
                ),
                expires_at=_parse_utc(str(raw.pop("expires_at"))),
            )
            if raw or len(result.data_key) != 32:
                raise ValueError("unwrap response is invalid")
        except (KeyError, TypeError, ValueError) as error:
            raise HippiusCanaryOperatorError(
                "canary unwrap helper response is invalid"
            ) from error
        return result

    def __repr__(self) -> str:
        return "ProcessHippiusPrivateInputUnwrapper(configured=True)"


class ProcessHippiusCanaryAuthoringExecutor:
    """Run authoring through its distinct protected helper."""

    def __init__(self, helper: ProtectedCanonicalHelper) -> None:
        self._helper = helper

    async def execute_authoring(
        self,
        *,
        material: HippiusShadowCanaryAuthoringMaterial,
    ) -> HippiusShadowCanaryAuthoringOutcome:
        raw = await self._helper.call(
            projection={
                "budgets": material.budgets.model_dump(mode="json", by_alias=True),
                "execution_authority_sha256": material.execution_authority_sha256,
                "issue": material.issue.model_dump(mode="json", by_alias=True),
                "phase": "authoring",
                "runner_plan": material.runner_plan.model_dump(
                    mode="json", by_alias=True
                ),
                "runtime_policy": material.runtime_policy.model_dump(
                    mode="json", by_alias=True
                ),
                "schema": _AUTHORING_REQUEST_SCHEMA,
                "task_commitment_sha256": material.task_commitment_sha256,
                "ticket_deadline": _utc_text(material.ticket_deadline),
                "weight_eligible": False,
            },
            deadline=material.ticket_deadline,
        )
        try:
            if set(raw) != {
                "execution_authority_sha256",
                "frozen_submission_b64",
                "resolved",
                "schema",
                "task_commitment_sha256",
                "transcript_b64",
                "weight_eligible",
            }:
                raise ValueError("authoring response fields are invalid")
            if raw.pop("schema") != _AUTHORING_RESPONSE_SCHEMA:
                raise ValueError("authoring response schema is invalid")
            if raw.pop("weight_eligible") is not False:
                raise ValueError("authoring response eligibility is invalid")
            result = HippiusShadowCanaryAuthoringOutcome(
                execution_authority_sha256=str(raw.pop("execution_authority_sha256")),
                task_commitment_sha256=str(raw.pop("task_commitment_sha256")),
                transcript=base64.b64decode(
                    str(raw.pop("transcript_b64")),
                    validate=True,
                ),
                frozen_submission=base64.b64decode(
                    str(raw.pop("frozen_submission_b64")),
                    validate=True,
                ),
                resolved=raw.pop("resolved") is True,
            )
            if raw:
                raise ValueError("authoring response is invalid")
        except (KeyError, TypeError, ValueError) as error:
            raise HippiusCanaryOperatorError(
                "canary authoring helper response is invalid"
            ) from error
        return result

    def __repr__(self) -> str:
        return "ProcessHippiusCanaryAuthoringExecutor(configured=True)"


class ProcessHippiusCanaryGradingExecutor:
    """Run pristine grading through a separate protected helper."""

    def __init__(self, helper: ProtectedCanonicalHelper) -> None:
        self._helper = helper

    async def execute_grading(
        self,
        *,
        material: HippiusShadowCanaryGradingMaterial,
    ) -> HippiusShadowCanaryGradingOutcome:
        raw = await self._helper.call(
            projection={
                "execution_authority_sha256": material.execution_authority_sha256,
                "frozen_submission_b64": base64.b64encode(
                    material.frozen_submission
                ).decode("ascii"),
                "frozen_submission_sha256": material.frozen_submission_sha256,
                "grader_plan": material.grader_plan.model_dump(
                    mode="json", by_alias=True
                ),
                "phase": "grading",
                "resource_profile": material.resource_profile.model_dump(
                    mode="json", by_alias=True
                ),
                "schema": _GRADING_REQUEST_SCHEMA,
                "task_commitment_sha256": material.task_commitment_sha256,
                "ticket_deadline": _utc_text(material.ticket_deadline),
                "weight_eligible": False,
            },
            deadline=material.ticket_deadline,
        )
        try:
            if set(raw) != {
                "execution_authority_sha256",
                "frozen_submission_sha256",
                "pristine",
                "resolved",
                "schema",
                "task_commitment_sha256",
                "terminal_evidence_b64",
                "weight_eligible",
            }:
                raise ValueError("grading response fields are invalid")
            if raw.pop("schema") != _GRADING_RESPONSE_SCHEMA:
                raise ValueError("grading response schema is invalid")
            if raw.pop("weight_eligible") is not False:
                raise ValueError("grading response eligibility is invalid")
            result = HippiusShadowCanaryGradingOutcome(
                execution_authority_sha256=str(raw.pop("execution_authority_sha256")),
                task_commitment_sha256=str(raw.pop("task_commitment_sha256")),
                frozen_submission_sha256=str(raw.pop("frozen_submission_sha256")),
                terminal_evidence=base64.b64decode(
                    str(raw.pop("terminal_evidence_b64")),
                    validate=True,
                ),
                resolved=raw.pop("resolved") is True,
                pristine=raw.pop("pristine") is True,
            )
            if raw:
                raise ValueError("grading response is invalid")
        except (KeyError, TypeError, ValueError) as error:
            raise HippiusCanaryOperatorError(
                "canary grading helper response is invalid"
            ) from error
        return result

    def __repr__(self) -> str:
        return "ProcessHippiusCanaryGradingExecutor(configured=True)"


class HippiusCanaryOperator:
    """Confirmation-gated single-process runner with an exclusive local lock."""

    def __init__(
        self,
        *,
        config: HippiusCanaryOperatorConfig,
        plan: HippiusShadowCanaryPlan,
        private_input: HippiusPrivateInputRetriever,
        evidence: HippiusEvidenceRuntime,
        authoring: ProcessHippiusCanaryAuthoringExecutor,
        grading: ProcessHippiusCanaryGradingExecutor,
        deployed_source_sha: str,
        repository_source_sha: str,
    ) -> None:
        if (
            _SOURCE_SHA.fullmatch(deployed_source_sha) is None
            or deployed_source_sha != repository_source_sha
            or deployed_source_sha != plan.source_sha
        ):
            raise HippiusCanaryOperatorError(
                "canary source does not match the deployed repository"
            )
        self._config = config
        self._plan = plan
        self._private_input = private_input
        self._evidence = evidence
        self._authoring = authoring
        self._grading = grading
        self._deployed_source_sha = deployed_source_sha

    async def run(
        self,
        *,
        confirmation: str,
        output: Path,
    ) -> tuple[str, str]:
        if confirmation != HIPPIUS_SHADOW_CANARY_CONFIRMATION:
            raise HippiusCanaryOperatorError("Hippius canary is not confirmed")
        _validate_new_receipt_output(output)
        with _operator_lock(self._config.helper_work_root):
            try:
                receipt = await run_hippius_shadow_canary(
                    plan=self._plan,
                    private_input=self._private_input,
                    evidence=self._evidence,
                    authoring=self._authoring,
                    grading=self._grading,
                    confirmation=confirmation,
                    deployed_source_sha=self._deployed_source_sha,
                )
                payload_sha256 = write_hippius_shadow_canary_receipt(
                    receipt=receipt,
                    output=output,
                )
            except HippiusShadowCanaryError as error:
                raise HippiusCanaryOperatorError(
                    "Hippius canary orchestration failed"
                ) from error
        return receipt.canary_run_sha256, payload_sha256

    def __repr__(self) -> str:
        return "HippiusCanaryOperator(enabled=True, one_shot=True)"


def parse_hippius_canary_operator_config(
    environ: Mapping[str, str] | None = None,
) -> HippiusCanaryOperatorConfig | None:
    values = os.environ if environ is None else environ
    raw_enabled = values.get(HIPPIUS_CANARY_OPERATOR_ENABLED, "false").lower()
    if raw_enabled in {"false", "0", "no", "off"}:
        return None
    if raw_enabled not in {"true", "1", "yes", "on"}:
        raise HippiusCanaryOperatorError(
            f"{HIPPIUS_CANARY_OPERATOR_ENABLED} must be true or false"
        )

    def required_path(name: str) -> Path:
        raw = values.get(name, "")
        path = Path(raw)
        if not raw or not path.is_absolute():
            raise HippiusCanaryOperatorError(
                f"required canary operator path is missing or relative: {name}"
            )
        return path

    try:
        timeout_seconds = int(
            values.get("DITTO_CODING_HIPPIUS_CANARY_HELPER_TIMEOUT_SECONDS", "7200")
        )
    except ValueError as error:
        raise HippiusCanaryOperatorError(
            "canary operator helper timeout is malformed"
        ) from error
    config = HippiusCanaryOperatorConfig(
        plan_path=required_path("DITTO_CODING_HIPPIUS_CANARY_PLAN_PATH"),
        deployed_source_path=required_path(
            "DITTO_CODING_HIPPIUS_CANARY_DEPLOYED_SOURCE_PATH"
        ),
        private_input_manifest_path=required_path(
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_MANIFEST_PATH"
        ),
        private_input_publication_receipt_path=required_path(
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_PUBLICATION_RECEIPT_PATH"
        ),
        curator_public_key_path=required_path(
            "DITTO_CODING_HIPPIUS_CURATOR_PUBLIC_KEY_PATH"
        ),
        unwrap_executable=required_path(
            "DITTO_CODING_HIPPIUS_CANARY_UNWRAP_EXECUTABLE"
        ),
        authoring_executable=required_path(
            "DITTO_CODING_HIPPIUS_CANARY_AUTHORING_EXECUTABLE"
        ),
        grading_executable=required_path(
            "DITTO_CODING_HIPPIUS_CANARY_GRADING_EXECUTABLE"
        ),
        helper_work_root=required_path("DITTO_CODING_HIPPIUS_CANARY_HELPER_WORK_ROOT"),
        helper_timeout_seconds=timeout_seconds,
    )
    if (
        not _MIN_HELPER_SECONDS <= config.helper_timeout_seconds <= _MAX_HELPER_SECONDS
        or len(
            {
                config.unwrap_executable,
                config.authoring_executable,
                config.grading_executable,
            }
        )
        != 3
    ):
        raise HippiusCanaryOperatorError("canary operator configuration is unsafe")
    return config


async def run_hippius_canary_operator_from_env(
    *,
    repository_root: Path,
    confirmation: str,
    output: Path,
) -> tuple[str, str]:
    """Compose the live one-shot runner only when every boundary is enabled."""

    config = parse_hippius_canary_operator_config()
    if config is None:
        raise HippiusCanaryOperatorError("Hippius canary operator is disabled")
    plan = load_hippius_shadow_canary_plan(config.plan_path)
    deployed_source_sha = _read_source_sha(config.deployed_source_path)
    repository_source_sha = resolve_clean_repository_source_sha(repository_root)
    _validate_owner_directory(config.helper_work_root, label="canary helper work root")
    unwrap_work_root = config.helper_work_root / "unwrap"
    authoring_work_root = config.helper_work_root / "authoring"
    grading_work_root = config.helper_work_root / "grading"
    for label, path in (
        ("canary unwrap work root", unwrap_work_root),
        ("canary authoring work root", authoring_work_root),
        ("canary grading work root", grading_work_root),
    ):
        _validate_owner_directory(path, label=label)
    _validate_distinct_executables(
        (
            config.unwrap_executable,
            config.authoring_executable,
            config.grading_executable,
        )
    )
    unwrap_helper = ProtectedCanonicalHelper(
        executable=config.unwrap_executable,
        work_root=unwrap_work_root,
        timeout_seconds=config.helper_timeout_seconds,
    )
    authoring_helper = ProtectedCanonicalHelper(
        executable=config.authoring_executable,
        work_root=authoring_work_root,
        timeout_seconds=config.helper_timeout_seconds,
    )
    grading_helper = ProtectedCanonicalHelper(
        executable=config.grading_executable,
        work_root=grading_work_root,
        timeout_seconds=config.helper_timeout_seconds,
    )
    engine = create_db_engine()
    try:
        session_maker = create_session_maker(engine)
        evidence = create_hippius_evidence_runtime_from_env(session_maker=session_maker)
        if evidence is None:
            raise HippiusCanaryOperatorError("Hippius evidence runtime is disabled")
        retrieval_config = parse_hippius_private_input_retrieval_config()
        async with AiobotoHippiusPrivateInputReader(retrieval_config) as reader:
            retriever = HippiusPrivateInputRetriever(
                config=retrieval_config,
                manifest_path=config.private_input_manifest_path,
                publication_receipt_path=(
                    config.private_input_publication_receipt_path
                ),
                curator_public_key_path=config.curator_public_key_path,
                reader=reader,
                unwrapper=ProcessHippiusPrivateInputUnwrapper(unwrap_helper),
            )
            operator = HippiusCanaryOperator(
                config=config,
                plan=plan,
                private_input=retriever,
                evidence=evidence,
                authoring=ProcessHippiusCanaryAuthoringExecutor(authoring_helper),
                grading=ProcessHippiusCanaryGradingExecutor(grading_helper),
                deployed_source_sha=deployed_source_sha,
                repository_source_sha=repository_source_sha,
            )
            return await operator.run(confirmation=confirmation, output=output)
    finally:
        await engine.dispose()


def load_hippius_shadow_canary_plan(path: Path) -> HippiusShadowCanaryPlan:
    body = _read_protected_file(
        path,
        maximum_bytes=_MAX_PLAN_BYTES,
        label="Hippius canary plan",
    )
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict):
            raise ValueError("plan root is invalid")
        if (
            coding_canonical_json_bytes(
                raw,
                maximum_bytes=_MAX_PLAN_BYTES,
                label="Hippius canary plan",
            )
            != body
        ):
            raise ValueError("plan is not canonical")
        if raw.pop("schema") != _PLAN_SCHEMA:
            raise ValueError("plan schema is invalid")
        if raw.pop("synthetic_only") is not True:
            raise ValueError("plan is not synthetic")
        if raw.pop("single_validator") is not True:
            raise ValueError("plan is not single-validator")
        if raw.pop("weight_eligible") is not False:
            raise ValueError("plan eligibility is invalid")
        private = _parse_private_input_authority(raw.pop("private_input"))
        sealed = _parse_sealed_evidence_authority(raw.pop("sealed_evidence"))
        plan = HippiusShadowCanaryPlan(
            canary_id=UUID(str(raw.pop("canary_id"))),
            source_sha=str(raw.pop("source_sha")),
            synthetic_corpus_release_id=str(raw.pop("synthetic_corpus_release_id")),
            synthetic_record_sha256=str(raw.pop("synthetic_record_sha256")),
            private_input=private,
            sealed_evidence=sealed,
            synthetic_only=True,
            single_validator=True,
            weight_eligible=False,
        )
        if raw:
            raise ValueError("plan fields are invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise HippiusCanaryOperatorError("Hippius canary plan is invalid") from error
    return plan


def resolve_clean_repository_source_sha(repository_root: Path) -> str:
    if not repository_root.is_absolute() or not repository_root.is_dir():
        raise HippiusCanaryOperatorError("canary repository source is unavailable")
    try:
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise HippiusCanaryOperatorError(
            "canary repository source is unavailable"
        ) from error
    if _SOURCE_SHA.fullmatch(source_sha) is None or dirty:
        raise HippiusCanaryOperatorError(
            "canary repository source is not an exact clean revision"
        )
    return source_sha


def _kill_helper_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None or process.pid is None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.returncode is None:
            process.kill()


async def _run_helper_process(
    *,
    executable: Path,
    work_root: Path,
    body: bytes,
    timeout_seconds: float,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=work_root,
            env=_HELPER_ENVIRONMENT,
            start_new_session=True,
        )
    except OSError as error:
        raise HippiusCanaryOperatorError("canary helper could not start") from error

    async def write_stdin() -> None:
        assert process.stdin is not None
        process.stdin.write(body)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

    async def read_stdout() -> bytes:
        assert process.stdout is not None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await process.stdout.read(64 << 10)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_HELPER_RESPONSE_BYTES:
                raise HippiusCanaryOperatorError(
                    "canary helper response exceeded its bound"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        async with asyncio.timeout(timeout_seconds):
            _stdin_task = asyncio.create_task(write_stdin())
            _stdout_task = asyncio.create_task(read_stdout())
            try:
                _, output = await asyncio.gather(_stdin_task, _stdout_task)
            finally:
                if not _stdin_task.done():
                    _stdin_task.cancel()
                if not _stdout_task.done():
                    _stdout_task.cancel()
            return_code = await process.wait()
    except asyncio.CancelledError:
        _kill_helper_group(process)
        await process.wait()
        raise
    except TimeoutError as error:
        _kill_helper_group(process)
        await process.wait()
        raise HippiusCanaryOperatorError("canary helper timed out") from error
    except HippiusCanaryOperatorError:
        _kill_helper_group(process)
        await process.wait()
        raise
    except (BrokenPipeError, OSError) as error:
        _kill_helper_group(process)
        await process.wait()
        raise HippiusCanaryOperatorError("canary helper transport failed") from error
    if return_code != 0:
        raise HippiusCanaryOperatorError("canary helper failed")
    return output


def _parse_private_input_authority(raw: object) -> HippiusPrivateInputTicketAuthority:
    if not isinstance(raw, dict):
        raise ValueError("private-input authority is invalid")
    values = dict(raw)
    commitment = CodingCatalogCommitment.model_validate(values.pop("commitment"))
    if values.pop("weight_eligible") is not False:
        raise ValueError("private-input eligibility is invalid")
    authority = HippiusPrivateInputTicketAuthority(
        ticket_id=UUID(str(values.pop("ticket_id"))),
        run_row_id=UUID(str(values.pop("run_row_id"))),
        validator_hotkey=str(values.pop("validator_hotkey")),
        coding_run_id=str(values.pop("coding_run_id")),
        assignment_sha256=str(values.pop("assignment_sha256")),
        run_manifest_sha256=str(values.pop("run_manifest_sha256")),
        ticket_deadline=_parse_utc(str(values.pop("ticket_deadline"))),
        delivery_phase=CodingArtifactDeliveryPhase(str(values.pop("delivery_phase"))),
        commitment=commitment,
        catalog_index=_strict_int(values.pop("catalog_index")),
        transport_manifest_sha256=str(values.pop("transport_manifest_sha256")),
        publication_receipt_payload_sha256=str(
            values.pop("publication_receipt_payload_sha256")
        ),
        weight_eligible=False,
    )
    if values:
        raise ValueError("private-input authority fields are invalid")
    return authority


def _parse_sealed_evidence_authority(
    raw: object,
) -> HippiusSealedEvidenceSourceAuthority:
    if not isinstance(raw, dict):
        raise ValueError("sealed-evidence authority is invalid")
    values = dict(raw)
    if values.pop("weight_eligible") is not False:
        raise ValueError("sealed-evidence eligibility is invalid")
    authority = HippiusSealedEvidenceSourceAuthority(
        ticket_id=UUID(str(values.pop("ticket_id"))),
        claim_generation=_strict_int(values.pop("claim_generation")),
        validator_hotkey=str(values.pop("validator_hotkey")),
        instance_id=str(values.pop("instance_id")),
        ticket_deadline=_parse_utc(str(values.pop("ticket_deadline"))),
        evidence_kind=CodingSealedEvidenceKind(str(values.pop("evidence_kind"))),
        weight_eligible=False,
    )
    if values:
        raise ValueError("sealed-evidence authority fields are invalid")
    return authority


def _validate_protected_executable(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise HippiusCanaryOperatorError("canary helper executable is unsafe")
    try:
        info = path.stat()
    except OSError as error:
        raise HippiusCanaryOperatorError(
            "canary helper executable is unavailable"
        ) from error
    self_owned = info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) in {
        0o500,
        0o700,
    }
    root_group_owned = (
        info.st_uid == 0
        and info.st_gid in os.getgroups()
        and stat.S_IMODE(info.st_mode) == 0o550
    )
    if not stat.S_ISREG(info.st_mode) or not (self_owned or root_group_owned):
        raise HippiusCanaryOperatorError("canary helper executable is unsafe")


def _validate_distinct_executables(paths: tuple[Path, Path, Path]) -> None:
    try:
        identities = {(path.stat().st_dev, path.stat().st_ino) for path in paths}
    except OSError as error:
        raise HippiusCanaryOperatorError(
            "canary helper executable is unavailable"
        ) from error
    if len(identities) != len(paths):
        raise HippiusCanaryOperatorError("canary helper executables are not distinct")


def _validate_owner_directory(path: Path, *, label: str) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise HippiusCanaryOperatorError(f"{label} is unsafe")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise HippiusCanaryOperatorError(f"{label} is unsafe")


def _validate_new_receipt_output(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise HippiusCanaryOperatorError("canary receipt output must be new")
    _validate_owner_directory(path.parent, label="canary receipt directory")


@contextmanager
def _operator_lock(root: Path) -> Iterator[None]:
    path = root / "operator.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise HippiusCanaryOperatorError(
            "another Hippius canary operator is active"
        ) from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise HippiusCanaryOperatorError("canary operator lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except HippiusCanaryOperatorError:
        os.close(descriptor)
        raise
    except (BlockingIOError, OSError) as error:
        os.close(descriptor)
        raise HippiusCanaryOperatorError(
            "another Hippius canary operator is active"
        ) from error
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_source_sha(path: Path) -> str:
    body = _read_protected_file(
        path,
        maximum_bytes=41,
        label="deployed source record",
    )
    try:
        value = body.decode("ascii").removesuffix("\n")
    except UnicodeDecodeError as error:
        raise HippiusCanaryOperatorError("deployed source record is invalid") from error
    if body != f"{value}\n".encode() or _SOURCE_SHA.fullmatch(value) is None:
        raise HippiusCanaryOperatorError("deployed source record is invalid")
    return value


def _read_protected_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HippiusCanaryOperatorError(f"{label} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 1 <= info.st_size <= maximum_bytes
        ):
            raise HippiusCanaryOperatorError(f"{label} is unsafe")
        chunks: list[bytes] = []
        size = 0
        while size < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as error:
        raise HippiusCanaryOperatorError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    body = b"".join(chunks)
    if not body or len(body) > maximum_bytes:
        raise HippiusCanaryOperatorError(f"{label} exceeds bounds")
    return body


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer field is invalid")
    return value


def _parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp is not UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HippiusCanaryOperatorError("canary timestamp is invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


__all__ = [
    "HIPPIUS_CANARY_OPERATOR_ENABLED",
    "HippiusCanaryOperator",
    "HippiusCanaryOperatorConfig",
    "HippiusCanaryOperatorError",
    "ProcessHippiusCanaryAuthoringExecutor",
    "ProcessHippiusCanaryGradingExecutor",
    "ProcessHippiusPrivateInputUnwrapper",
    "ProtectedCanonicalHelper",
    "load_hippius_shadow_canary_plan",
    "parse_hippius_canary_operator_config",
    "resolve_clean_repository_source_sha",
    "run_hippius_canary_operator_from_env",
]
