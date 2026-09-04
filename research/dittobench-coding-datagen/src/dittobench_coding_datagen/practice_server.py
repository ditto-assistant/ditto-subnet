"""Loopback capability server and harness evaluator for public practice."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.model import CODING_CONTRACT_VERSION, CorpusError
from dittobench_coding_datagen.practice_grader import (
    PracticeTaskEvidence,
    grade_frozen_practice_submission,
)
from dittobench_coding_datagen.practice_runtime import (
    MAX_TOOL_BODY_BYTES,
    PracticeWorkspaceSession,
    ToolRequest,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_REQUIRED_CAPABILITIES = (
    "case_scoped_inference_v1",
    "coding_runner_tools_v1",
    "scoped_memory_seed_v1",
)


def _json_response(
    handler: BaseHTTPRequestHandler, status: HTTPStatus, value: Any
) -> None:
    body = canonical_json_bytes(value)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class _CapabilityHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, session: PracticeWorkspaceSession, token: str) -> None:
        self.session = session
        self.capability_path = f"/v1/practice/{token}/tool"
        super().__init__(("127.0.0.1", 0), _CapabilityHandler)


class _CapabilityHandler(BaseHTTPRequestHandler):
    server: _CapabilityHTTPServer

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != self.server.capability_path:
            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            length = -1
        if length < 1 or length > MAX_TOOL_BODY_BYTES:
            _json_response(
                self,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "invalid tool request size"},
            )
            return
        try:
            value = json.loads(self.rfile.read(length))
            request = ToolRequest.from_json(value)
            response = self.server.session.invoke(request)
        except (CorpusError, json.JSONDecodeError) as error:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        _json_response(self, HTTPStatus.OK, response.as_json())


class PracticeCapabilityServer:
    """One random loopback capability bound to one workspace session."""

    def __init__(self, session: PracticeWorkspaceSession) -> None:
        self._httpd = _CapabilityHTTPServer(session, secrets.token_urlsafe(32))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"practice-tools-{session.case.task_id}",
            daemon=True,
        )
        self._started = False

    @property
    def capability_url(self) -> str:
        port = int(self._httpd.server_address[1])
        return f"http://127.0.0.1:{port}{self._httpd.capability_path}"

    def start(self) -> None:
        if self._started:
            raise CorpusError("practice capability server already started")
        self._thread.start()
        self._started = True

    def close(self) -> None:
        if self._started:
            self._httpd.shutdown()
            self._thread.join(timeout=5)
            self._started = False
        self._httpd.server_close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def _loopback_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise CorpusError("public practice harness URL must be loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CorpusError("public practice harness URL contains unsupported fields")
    try:
        port = parsed.port
    except ValueError as error:
        raise CorpusError("public practice harness URL has an invalid port") from error
    if port is None:
        raise CorpusError("public practice harness URL must include a port")
    return value.rstrip("/")


def _request_json(
    method: str, url: str, value: Any | None, *, timeout_seconds: int
) -> Any:
    body = None if value is None else canonical_json_bytes(value)
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read(MAX_TOOL_BODY_BYTES + 1)
    except urllib.error.HTTPError as error:
        detail = error.read(MAX_TOOL_BODY_BYTES).decode("utf-8", errors="replace")
        raise CorpusError(
            f"practice harness returned HTTP {error.code}: {detail}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise CorpusError(f"practice harness request failed: {error}") from error
    if len(response_body) > MAX_TOOL_BODY_BYTES:
        raise CorpusError("practice harness response exceeds the output limit")
    try:
        return json.loads(response_body)
    except json.JSONDecodeError as error:
        raise CorpusError("practice harness returned invalid JSON") from error


def _validate_health(value: Any) -> None:
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise CorpusError("practice harness health response is not ready")
    versions = value.get("supported_coding_contract_versions")
    if not isinstance(versions, list) or CODING_CONTRACT_VERSION not in versions:
        raise CorpusError("practice harness does not support coding contract v1")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, list) or any(
        capability not in capabilities for capability in _REQUIRED_CAPABILITIES
    ):
        raise CorpusError("practice harness health lacks required coding capabilities")


def _validate_seed_response(
    value: Any,
    seed: dict[str, Any],
    expected_digest: str,
    expected_count: int,
) -> None:
    if not isinstance(value, dict):
        raise CorpusError("practice harness seed response must be an object")
    if (
        value.get("case_id") != seed.get("case_id")
        or value.get("profile_capability_id") != seed.get("profile_capability_id")
        or value.get("idempotent_replay") is not False
    ):
        raise CorpusError(
            "practice harness seed response does not echo request identity"
        )
    if value.get("memory_bundle_sha256") != expected_digest:
        raise CorpusError("practice harness did not confirm the memory bundle digest")
    if value.get("memory_count") != expected_count:
        raise CorpusError("practice harness did not confirm the memory bundle count")


def _validate_run_response(value: Any, case_id: str) -> None:
    if not isinstance(value, dict) or value.get("case_id") != case_id:
        raise CorpusError("practice harness run response has the wrong case")
    final_report = value.get("final_report")
    if not isinstance(final_report, dict):
        raise CorpusError("practice harness run response lacks a final report")
    summary = final_report.get("summary")
    risks = final_report.get("remaining_risks")
    if not isinstance(summary, str) or not summary or len(summary) > 2_000:
        raise CorpusError("practice harness final summary is invalid")
    if (
        not isinstance(risks, list)
        or len(risks) > 32
        or any(
            not isinstance(risk, str) or not risk or len(risk) > 2_000 for risk in risks
        )
    ):
        raise CorpusError("practice harness remaining risks are invalid")


def evaluate_practice_harness(
    pack: Path,
    task_id: str,
    harness_base_url: str,
    *,
    inference_base_url: str = "http://127.0.0.1:9/offline-disabled",
    timeout_seconds: int = 120,
) -> PracticeTaskEvidence:
    """Evaluate one loopback harness against one public practice task."""

    if timeout_seconds < 1 or timeout_seconds > 300:
        raise CorpusError("practice harness timeout must be between 1 and 300 seconds")
    harness = _loopback_base_url(harness_base_url)
    with PracticeWorkspaceSession(pack, task_id) as session:
        with PracticeCapabilityServer(session) as tools:
            health = _request_json(
                "GET", f"{harness}/coding/health", None, timeout_seconds=timeout_seconds
            )
            _validate_health(health)
            seed = session.case.seed_request()
            seed_response = _request_json(
                "POST",
                f"{harness}/coding/seed",
                seed,
                timeout_seconds=timeout_seconds,
            )
            _validate_seed_response(
                seed_response,
                seed,
                session.case.memory_bundle_sha256,
                len(session.case.memories),
            )
            run = session.case.run_request(
                workspace_capability_url=tools.capability_url,
                inference_base_url=inference_base_url,
            )
            harness_completed = True
            try:
                run_response = _request_json(
                    "POST",
                    f"{harness}/coding/run",
                    run,
                    timeout_seconds=timeout_seconds,
                )
                _validate_run_response(run_response, task_id)
            except CorpusError:
                harness_completed = False
            try:
                submission = session.freeze()
            except CorpusError:
                failed = session.freeze_failure_identity()
                return PracticeTaskEvidence(
                    coding_contract_version=CODING_CONTRACT_VERSION,
                    weight_eligible=False,
                    task_id=failed.task_id,
                    base_tree_sha256=failed.base_tree_sha256,
                    final_tree_sha256=failed.final_tree_sha256,
                    patch_sha256="0" * 64,
                    changed_path_root=failed.changed_path_root,
                    authoring_event_root=failed.authoring_event_root,
                    build_returncode=126,
                    visible_tests_returncode=126,
                    grader_tests_returncode=126,
                    protected_paths_intact=False,
                    harness_completed=False,
                    terminal_domain="harness_failure",
                    repair_score_micros=0,
                )
        evidence = grade_frozen_practice_submission(pack, submission)
        if harness_completed:
            return evidence
        return replace(
            evidence,
            harness_completed=False,
            terminal_domain="harness_failure",
            repair_score_micros=0,
        )
