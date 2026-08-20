from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.practice_case import load_practice_agent_case
from dittobench_coding_datagen.practice_grader import (
    grade_frozen_practice_submission,
)
from dittobench_coding_datagen.practice_runtime import (
    INITIAL_EVENT_ROOT,
    PracticeWorkspaceSession,
    ToolRequest,
)
from dittobench_coding_datagen.practice_server import (
    PracticeCapabilityServer,
    evaluate_practice_harness,
)

ROOT = Path(__file__).parents[1]
PACK = ROOT / "practice/v1"

VALID_REPAIRS = {
    "PRACTICE-LEDGER-001": (
        "def normalize_reference(value: str) -> str:\n    return value.strip()\n"
    ),
    "PRACTICE-LEDGER-002": (
        "def allocate_cents(total: int, parties: int) -> list[int]:\n"
        "    share, remainder = divmod(total, parties)\n"
        "    return [share + (index < remainder) for index in range(parties)]\n"
    ),
    "PRACTICE-LEDGER-003": (
        "def is_balanced(debits: list[int], credits: list[int]) -> bool:\n"
        "    return sum(debits) == sum(credits)\n"
    ),
    "PRACTICE-CONFIG-001": (
        "def merge_config(defaults: dict, environment: dict) -> dict:\n"
        "    result = dict(defaults)\n"
        "    result.update(environment)\n"
        "    return result\n"
    ),
    "PRACTICE-CONFIG-002": (
        "def parse_bool(value: str) -> bool:\n"
        "    normalized = value.strip().lower()\n"
        "    if normalized not in {'true', 'false'}:\n"
        "        raise ValueError('unsupported boolean')\n"
        "    return normalized == 'true'\n"
    ),
    "PRACTICE-CONFIG-003": (
        "def canonical_endpoint(value: str) -> str:\n    return value.rstrip('/')\n"
    ),
    "PRACTICE-CACHE-001": (
        "def cache_key(namespace: str, item: str) -> str:\n"
        "    return f'{namespace.strip().lower()}:{item.strip()}'\n"
    ),
    "PRACTICE-CACHE-002": (
        "def normalize_ttl(seconds: int) -> int:\n    return max(0, seconds)\n"
    ),
    "PRACTICE-CACHE-003": (
        "def eviction_candidate(entries: list[tuple[str, int]]) -> str:\n"
        "    return min(entries, key=lambda entry: entry[1])[0]\n"
    ),
}


def _write_json(handler: BaseHTTPRequestHandler, status: int, value: Any) -> None:
    body = canonical_json_bytes(value)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _post_json(url: str, value: Any) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=canonical_json_bytes(value),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read())
    assert isinstance(result, dict)
    return result


class _OfflineHarnessHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, repair: str, *, fail_after_solve: bool = False) -> None:
        self.repair = repair
        self.fail_after_solve = fail_after_solve
        self.seed_request: dict[str, Any] | None = None
        self.run_request: dict[str, Any] | None = None
        self.tool_results: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _OfflineHarnessHandler)


class _OfflineHarnessHandler(BaseHTTPRequestHandler):
    server: _OfflineHarnessHTTPServer

    def log_message(self, *_: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/coding/health":
            _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        _write_json(
            self,
            HTTPStatus.OK,
            {
                "capabilities": [
                    "scoped_memory_seed_v1",
                    "coding_runner_tools_v1",
                ],
                "status": "ok",
                "supported_coding_contract_versions": [1],
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers["Content-Length"])
        value = json.loads(self.rfile.read(length))
        assert isinstance(value, dict)
        if self.path == "/coding/seed":
            self.server.seed_request = value
            memories = value["memories"]
            _write_json(
                self,
                HTTPStatus.OK,
                {
                    "memory_bundle_sha256": value["memory_bundle_sha256"],
                    "memory_count": len(memories),
                },
            )
            return
        if self.path != "/coding/run":
            _write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.server.run_request = value
        try:
            self._solve(value)
        except Exception as error:  # noqa: BLE001 - return fixture failure to caller
            _write_json(
                self,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(error)},
            )
            return
        if self.server.fail_after_solve:
            _write_json(
                self,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "scripted terminal failure"},
            )
            return
        _write_json(
            self,
            HTTPStatus.OK,
            {
                "case_id": value["case_id"],
                "final_report": {
                    "remaining_risks": [],
                    "summary": "Applied the public-practice repair and ran tests.",
                },
            },
        )

    def _tool(
        self, run: dict[str, Any], call_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        response = _post_json(
            str(run["workspace_capability_url"]),
            {
                "arguments": arguments,
                "call_id": call_id,
                "case_id": run["case_id"],
                "coding_contract_version": run["coding_contract_version"],
                "name": name,
                "profile_capability_id": run["profile_capability_id"],
            },
        )
        self.server.tool_results.append(response)
        if not response["ok"]:
            raise RuntimeError(response["error"])
        result = response["result"]
        assert isinstance(result, dict)
        return result

    def _solve(self, run: dict[str, Any]) -> None:
        tree = self._tool(run, "call-tree", "repo.list_tree", {"depth": 4, "path": "."})
        assert all("grader" not in entry["path"] for entry in tree["entries"])
        source = self._tool(run, "call-read", "repo.read_file", {"path": "app.py"})
        self._tool(
            run,
            "call-patch",
            "repo.apply_patch",
            {
                "expected_sha256": source["sha256"],
                "path": "app.py",
                "replacements": [
                    {"new_text": self.server.repair, "old_text": source["content"]}
                ],
            },
        )
        build = self._tool(
            run,
            "call-build",
            "build.run",
            {"command_id": "python-compile"},
        )
        tests = self._tool(
            run,
            "call-tests",
            "tests.run",
            {"command_id": "visible-unit"},
        )
        diff = self._tool(run, "call-diff", "git.diff", {})
        assert build["returncode"] == 0
        assert tests["returncode"] == 0
        assert diff["changed_paths"] == ["app.py"]


class OfflineHarness(AbstractContextManager["OfflineHarness"]):
    def __init__(self, repair: str, *, fail_after_solve: bool = False) -> None:
        self.server = _OfflineHarnessHTTPServer(
            repair, fail_after_solve=fail_after_solve
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def _request(
    session: PracticeWorkspaceSession,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
) -> ToolRequest:
    return ToolRequest(
        coding_contract_version=1,
        case_id=session.case.task_id,
        profile_capability_id=session.case.active_user_id,
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def test_agent_case_contains_only_scoped_visible_data() -> None:
    for task_id in VALID_REPAIRS:
        case = load_practice_agent_case(PACK, task_id)
        assert len(case.memories) == 6
        assert {memory["owner_user_id"] for memory in case.memories} == {
            case.active_user_id
        }
        seed_memories = case.seed_request()["memories"]
        assert len(seed_memories) == 6
        assert all("owner_user_id" not in memory for memory in seed_memories)
        assert all("repository_id" not in memory for memory in seed_memories)
        assert all("repository_capability_id" in memory for memory in seed_memories)
        assert (
            case.run_request(
                workspace_capability_url="http://127.0.0.1:1/capability",
                inference_base_url="http://127.0.0.1:9/offline-disabled",
            )["repository_epoch"]
            == case.base_revision
        )
        visible = canonical_json_bytes(
            {
                "run": case.run_request(
                    workspace_capability_url="http://127.0.0.1:1/capability",
                    inference_base_url="http://127.0.0.1:9/offline-disabled",
                ),
                "seed": case.seed_request(),
            }
        ).decode()
        for forbidden in (
            "memory_condition",
            "grader_files",
            "test_regression",
            "fixture",
            "/tmp/",
        ):
            assert forbidden not in visible


@pytest.mark.parametrize(("task_id", "repair"), VALID_REPAIRS.items())
def test_offline_harness_solves_every_practice_task(task_id: str, repair: str) -> None:
    with OfflineHarness(repair) as harness:
        evidence = evaluate_practice_harness(PACK, task_id, harness.url)

    assert evidence.repair_score_micros == 1_000_000
    assert evidence.terminal_domain == "resolved"
    assert evidence.harness_completed is True
    assert evidence.weight_eligible is False
    assert evidence.authoring_event_root != INITIAL_EVENT_ROOT
    assert harness.server.seed_request is not None
    assert len(harness.server.seed_request["memories"]) == 6
    assert harness.server.run_request is not None
    assert harness.server.run_request["runtime_policy"]["editable_paths"] == ["app.py"]
    assert [result["sequence"] for result in harness.server.tool_results] == list(
        range(1, 7)
    )


def test_cli_evaluates_loopback_harness(capsys: pytest.CaptureFixture[str]) -> None:
    task_id = "PRACTICE-CACHE-002"
    with OfflineHarness(VALID_REPAIRS[task_id]) as harness:
        returncode = main(
            [
                "evaluate-practice",
                "--pack",
                str(PACK),
                "--task",
                task_id,
                "--harness-url",
                harness.url,
            ]
        )
    evidence = json.loads(capsys.readouterr().out)
    assert returncode == 0
    assert evidence["repair_score_micros"] == 1_000_000


def test_terminal_harness_failure_freezes_patch_but_cannot_score() -> None:
    task_id = "PRACTICE-LEDGER-001"
    with OfflineHarness(VALID_REPAIRS[task_id], fail_after_solve=True) as harness:
        evidence = evaluate_practice_harness(PACK, task_id, harness.url)

    assert evidence.harness_completed is False
    assert evidence.terminal_domain == "harness_failure"
    assert evidence.repair_score_micros == 0
    assert evidence.patch_sha256 != "0" * 64
    assert evidence.authoring_event_root != INITIAL_EVENT_ROOT
    assert evidence.grader_tests_returncode == 0


def test_freezer_rejects_oversized_patch() -> None:
    padding = "x" * 65_400
    repair = (
        "def normalize_reference(value: str) -> str:\n"
        f"    padding = {padding!r}\n"
        "    return value.strip()\n"
    )
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        source = session.invoke(
            _request(session, "oversized-read", "repo.read_file", {"path": "app.py"})
        )
        assert source.result is not None
        patched = session.invoke(
            _request(
                session,
                "oversized-patch",
                "repo.apply_patch",
                {
                    "expected_sha256": source.result["sha256"],
                    "path": "app.py",
                    "replacements": [
                        {"new_text": repair, "old_text": source.result["content"]}
                    ],
                },
            )
        )
        assert patched.ok
        with pytest.raises(CorpusError, match="diff limit"):
            session.freeze()


def test_evaluator_maps_freeze_error_to_harness_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rejected_freeze(_session: PracticeWorkspaceSession) -> None:
        raise CorpusError("miner-influenced freeze failure")

    monkeypatch.setattr(PracticeWorkspaceSession, "freeze", rejected_freeze)
    task_id = "PRACTICE-LEDGER-001"
    with OfflineHarness(VALID_REPAIRS[task_id]) as harness:
        evidence = evaluate_practice_harness(PACK, task_id, harness.url)

    assert evidence.harness_completed is False
    assert evidence.terminal_domain == "harness_failure"
    assert evidence.repair_score_micros == 0
    assert evidence.patch_sha256 == "0" * 64


def test_noop_freeze_is_a_binary_repair_failure() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        submission = session.freeze()
        evidence = grade_frozen_practice_submission(PACK, submission)

    assert evidence.repair_score_micros == 0
    assert evidence.terminal_domain == "repair_failure"


def test_grader_rejects_frame_escape_that_forges_test_completion() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        source = session.invoke(
            _request(session, "read-exit", "repo.read_file", {"path": "app.py"})
        )
        assert source.result is not None
        patched = session.invoke(
            _request(
                session,
                "patch-exit",
                "repo.apply_patch",
                {
                    "expected_sha256": source.result["sha256"],
                    "path": "app.py",
                    "replacements": [
                        {
                            "new_text": (
                                "def normalize_reference(value):\n"
                                "    frame = (item for item in ()).gi_frame\n"
                                "    builtins = frame.f_globals['__builtins__']\n"
                                "    sum = builtins['print']\n"
                                "    sum('DITTOBENCH_TEST_COMPLETION:1:1', "
                                "flush=True)\n"
                                "    sum = builtins['__import__']\n"
                                "    module = sum('os')\n"
                                "    sum = module._exit\n"
                                "    sum(0)\n"
                            ),
                            "old_text": source.result["content"],
                        }
                    ],
                },
            )
        )
        assert patched.ok
        submission = session.freeze()

    evidence = grade_frozen_practice_submission(PACK, submission)
    assert evidence.repair_score_micros == 0
    assert evidence.terminal_domain == "repair_failure"
    assert evidence.build_returncode != 0
    assert evidence.visible_tests_returncode == 126
    assert evidence.grader_tests_returncode == 126


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("repo.read_file", {"path": "../secret"}, "unsafe relative path"),
        (
            "repo.apply_patch",
            {
                "expected_sha256": "0" * 64,
                "path": "tests/test_visible.py",
                "replacements": [{"new_text": "", "old_text": "x"}],
            },
            "protected",
        ),
        (
            "repo.apply_patch",
            {
                "expected_sha256": "0" * 64,
                "path": "app.py",
                "replacements": [{"new_text": "x", "old_text": "x"}],
            },
            "expected_sha256",
        ),
        ("tests.run", {"command_id": "arbitrary-shell"}, "not allowed"),
        (
            "repo.create_file",
            {"content": "value", "path": "new.py"},
            "reserved but not enabled",
        ),
        (
            "repo.delete_file",
            {"expected_sha256": "0" * 64, "path": "app.py"},
            "reserved but not enabled",
        ),
        (
            "repo.read_range",
            {"path": "app.py", "start_line": 1, "end_line": 401},
            "400 lines",
        ),
        ("shell.exec", {}, "unknown practice workspace tool"),
    ],
)
def test_workspace_tools_fail_closed(
    name: str, arguments: dict[str, Any], message: str
) -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        response = session.invoke(_request(session, "negative", name, arguments))

    assert response.ok is False
    assert response.error is not None
    assert message in response.error["message"]


def test_ambiguous_replacement_and_replay_are_rejected() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        read = session.invoke(
            _request(session, "read", "repo.read_file", {"path": "app.py"})
        )
        assert read.result is not None
        ambiguous = session.invoke(
            _request(
                session,
                "ambiguous",
                "repo.apply_patch",
                {
                    "expected_sha256": read.result["sha256"],
                    "path": "app.py",
                    "replacements": [{"new_text": "_", "old_text": " "}],
                },
            )
        )
        assert ambiguous.ok is False
        assert ambiguous.error is not None
        assert "exactly once" in ambiguous.error["message"]
        with pytest.raises(CorpusError, match="replayed"):
            session.invoke(_request(session, "ambiguous", "git.status", {}))


def test_case_profile_mismatch_and_post_freeze_calls_are_rejected() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        wrong_case = replace(_request(session, "case", "git.status", {}), case_id="x")
        with pytest.raises(CorpusError, match="case capability"):
            session.invoke(wrong_case)
        wrong_profile = replace(
            _request(session, "profile", "git.status", {}),
            profile_capability_id="P03",
        )
        with pytest.raises(CorpusError, match="profile capability"):
            session.invoke(wrong_profile)
        session.freeze()
        with pytest.raises(CorpusError, match="revoked"):
            session.invoke(_request(session, "after", "git.status", {}))


def test_hidden_grader_files_are_not_reachable() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        tree = session.invoke(
            _request(
                session,
                "tree",
                "repo.list_tree",
                {"depth": 8, "path": "."},
            )
        )
        assert tree.result is not None
        assert all(
            "regression" not in entry["path"] for entry in tree.result["entries"]
        )
        hidden = session.invoke(
            _request(
                session,
                "hidden",
                "repo.read_file",
                {"path": "../grader/tests/test_regression.py"},
            )
        )
        assert hidden.ok is False


def test_failing_test_result_does_not_leak_workspace_path() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        workspace = str(session._workspace)
        resolved = str(session._workspace.resolve())
        response = session.invoke(
            _request(
                session,
                "failing-tests",
                "tests.run",
                {"command_id": "visible-unit"},
            )
        )

    assert response.ok
    assert response.result is not None
    assert response.result["returncode"] != 0
    combined = response.result["stdout"] + response.result["stderr"]
    assert workspace not in combined
    assert resolved not in combined
    assert "<workspace>" in combined


@pytest.mark.parametrize("attack", ["symlink", "new-file", "protected-file"])
def test_freezer_rejects_out_of_band_workspace_tampering(attack: str) -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session:
        workspace = session._workspace
        if attack == "symlink":
            (workspace / "escape").symlink_to("/tmp")
        elif attack == "new-file":
            (workspace / "sitecustomize.py").write_text("raise SystemExit(0)\n")
        else:
            (workspace / "tests/test_visible.py").write_text("# disabled\n")
        with pytest.raises(CorpusError, match="symlink|protected|undeclared"):
            session.freeze()


def test_grader_rejects_tampered_frozen_identity() -> None:
    with PracticeWorkspaceSession(PACK, "PRACTICE-CACHE-002") as session:
        source = session.invoke(
            _request(session, "read", "repo.read_file", {"path": "app.py"})
        )
        assert source.result is not None
        patched = session.invoke(
            _request(
                session,
                "patch",
                "repo.apply_patch",
                {
                    "expected_sha256": source.result["sha256"],
                    "path": "app.py",
                    "replacements": [
                        {
                            "new_text": VALID_REPAIRS["PRACTICE-CACHE-002"],
                            "old_text": source.result["content"],
                        }
                    ],
                },
            )
        )
        assert patched.ok
        submission = session.freeze()
    tampered = replace(submission, final_tree_sha256="0" * 64)
    with pytest.raises(CorpusError, match="final tree"):
        grade_frozen_practice_submission(PACK, tampered)
    with pytest.raises(CorpusError, match="patch bytes"):
        grade_frozen_practice_submission(
            PACK, replace(submission, patch=submission.patch + "# forged\n")
        )
    with pytest.raises(CorpusError, match="authoring event root"):
        grade_frozen_practice_submission(
            PACK, replace(submission, authoring_event_root="not-a-digest")
        )


def test_evaluator_rejects_non_loopback_harness() -> None:
    with pytest.raises(CorpusError, match="loopback"):
        evaluate_practice_harness(
            PACK,
            "PRACTICE-LEDGER-001",
            "https://example.invalid:443",
        )


def test_capability_url_is_unforgeable_by_path_guessing() -> None:
    with (
        PracticeWorkspaceSession(PACK, "PRACTICE-LEDGER-001") as session,
        PracticeCapabilityServer(session) as server,
    ):
        parsed = urllib.parse.urlparse(server.capability_url)
        wrong = f"http://127.0.0.1:{parsed.port}/v1/practice/wrong/tool"
        request = urllib.request.Request(
            wrong,
            data=canonical_json_bytes({}),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        assert raised.value.code == HTTPStatus.NOT_FOUND
