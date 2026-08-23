from argparse import Namespace

from screener_capacity.targon import TargonAPIError
from screener_capacity.targon_cli import (
    _cleanup_probe_workload,
    _source_review_mock_script,
    _source_review_probe_archive,
    _source_review_starter_kit_mock_script,
    command_agent_probe,
    command_kaniko_probe,
    command_logs,
    command_source_review_probe,
    command_state,
)


class _Targon:
    def __init__(
        self,
        *,
        status: str = "running",
        delete_fails: bool = True,
        exec_fails: bool = False,
    ) -> None:
        self.status = status
        self.delete_fails = delete_fails
        self.exec_fails = exec_fails
        self.suspended: list[str] = []
        self.deleted: list[str] = []
        self.created: list[dict[str, object]] = []

    def inventory(self) -> list[dict[str, object]]:
        return [{"name": "cpu-small", "available": 1}]

    def create_rental(self, **values: object) -> dict[str, str]:
        self.created.append(values)
        return {"uid": "wrk-probe"}

    def deploy(self, _uid: str) -> dict[str, object]:
        return {}

    def update(self, uid: str, **values: object) -> dict[str, object]:
        del uid, values
        return {}

    def state(self, uid: str) -> dict[str, object]:
        return {
            "uid": uid,
            "status": self.status,
            "ready_replicas": 1 if self.status == "running" else 0,
            "total_replicas": 1 if self.status == "running" else 0,
        }

    def exec(self, _uid: str, _command: list[str]) -> str:
        if self.exec_fails:
            raise TargonAPIError(operation="POST exec", status=500, reason="HTTP error")
        return '["name=rootless"]'

    def logs(self, _uid: str, *, tail: int) -> str:
        assert tail > 0
        return "opencode=1.18.18\npi=0.73.1\nAGENT_RUNTIME_AVAILABLE"

    def suspend(self, uid: str) -> dict[str, object]:
        self.suspended.append(uid)
        self.status = "suspended"
        return {}

    def delete(self, uid: str) -> None:
        self.deleted.append(uid)
        if self.delete_fails:
            raise TargonAPIError(operation="DELETE", status=500, reason="HTTP error")


def test_cleanup_failure_is_reported_without_masking_probe_result() -> None:
    client = _Targon(status="running")

    result = _cleanup_probe_workload(client, "wrk-probe")

    assert result == {
        "phase": "cleanup-required",
        "uid": "wrk-probe",
        "deleted": False,
        "suspended": False,
        "status": "running",
    }
    assert client.deleted == ["wrk-probe", "wrk-probe"]
    assert client.suspended == []


def test_lost_delete_response_reconciles_deleted_state() -> None:
    client = _Targon(status="deleted")

    result = _cleanup_probe_workload(client, "wrk-probe")

    assert result["phase"] == "deleted"
    assert result["deleted"] is True
    assert client.suspended == []


def test_agent_probe_pins_binaries_and_cleans_up(monkeypatch, capsys) -> None:
    client = _Targon(delete_fails=False)
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    args = Namespace(
        resource="cpu-small",
        image="node:22-bookworm-slim",
        opencode_version="1.18.18",
        pi_version="0.73.1",
        provision_timeout_seconds=1,
        keep=False,
    )

    assert command_agent_probe(args) == 0

    assert len(client.created) == 1
    command = str(client.created[0]["args"])
    assert "opencode-ai@1.18.18" in command
    assert "@mariozechner/pi-coding-agent@0.73.1" in command
    output = capsys.readouterr().out
    assert '"capability": "AVAILABLE"' in output
    assert '"phase": "deleted"' in output


def test_kaniko_roundtrip_uses_runtime_marker_when_ready_replicas_are_zero(
    monkeypatch, capsys
) -> None:
    client = _Targon(delete_fails=False)

    def create_rental(**values: object) -> dict[str, str]:
        client.created.append(values)
        uid = f"wrk-probe-{len(client.created)}"
        return {"uid": uid}

    def state(uid: str) -> dict[str, object]:
        return {
            "uid": uid,
            "status": "running",
            "ready_replicas": 0,
            "total_replicas": 0,
        }

    def logs(uid: str, *, tail: int) -> str:
        assert tail > 0
        if uid == "wrk-probe-1":
            return "KANIKO_PROBE_AVAILABLE"
        return "targon-kaniko-ok"

    monkeypatch.setattr(client, "create_rental", create_rental)
    monkeypatch.setattr(client, "state", state)
    monkeypatch.setattr(client, "logs", logs)
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    args = Namespace(
        resource="cpu-small",
        image="kaniko:test",
        provision_timeout_seconds=1,
        roundtrip=True,
        starter_kit_sha=None,
        screen_contract=False,
        keep=False,
    )

    assert command_kaniko_probe(args) == 0

    output = capsys.readouterr().out
    assert '"phase": "runtime-executed"' in output
    assert '"capability": "AVAILABLE"' in output
    assert client.deleted == ["wrk-probe-2", "wrk-probe-1"]


def test_source_review_probe_runs_exact_job_and_cleans_up(monkeypatch, capsys) -> None:
    class SourceReviewTargon(_Targon):
        def __init__(self) -> None:
            super().__init__(delete_fails=False)
            self.updated: list[tuple[str, list[dict[str, str]]]] = []

        def inventory(self) -> list[dict[str, object]]:
            return [{"name": "cpu-small", "available": 2}]

        def create_rental(self, **values: object) -> dict[str, str]:
            self.created.append(values)
            return {"uid": f"wrk-probe-{len(self.created)}"}

        def state(self, uid: str) -> dict[str, object]:
            state = super().state(uid)
            if uid == "wrk-probe-1":
                state["urls"] = [
                    {
                        "port": 8080,
                        "url": "https://source-review-mock.example",
                    }
                ]
            return state

        def update(self, uid: str, *, envs: list[dict[str, str]]) -> dict[str, object]:
            self.updated.append((uid, envs))
            return {}

        def logs(self, uid: str, *, tail: int) -> str:
            assert tail > 0
            if uid == "wrk-probe-1":
                return (
                    'SOURCE_REVIEW_COMPLETE={"categories": ["none"], '
                    '"clearance_certified": true, "ok": true, '
                    '"risk_level": "low"}'
                )
            return ""

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    client = SourceReviewTargon()
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr(
        "screener_capacity.targon_cli.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(
        "screener_capacity.targon_cli.time.sleep", lambda _seconds: None
    )
    args = Namespace(
        resource="cpu-small",
        image="registry.example/screener@sha256:" + "a" * 64,
        provision_timeout_seconds=1,
        review_timeout_seconds=30,
        keep=False,
    )

    assert command_source_review_probe(args) == 0

    assert len(client.created) == 2
    assert client.created[1]["args"] == ["ditto_screener.source_review_job"]
    env = {row["name"]: row["value"] for row in client.created[1]["envs"]}
    assert env["SCREENER_SOURCE_REVIEW_API_KEY"] == (
        "probe-openrouter-key-not-a-secret"
    )
    assert client.deleted == ["wrk-probe-2", "wrk-probe-1"]
    assert client.updated == []
    output = capsys.readouterr().out
    assert '"phase": "source-review"' in output
    assert '"capability": "AVAILABLE"' in output


def test_live_model_source_review_probe_pins_layered_env(
    monkeypatch, capsys
) -> None:
    class LiveSourceReviewTargon(_Targon):
        def inventory(self) -> list[dict[str, object]]:
            return [{"name": "cpu-small", "available": 2}]

        def create_rental(self, **values: object) -> dict[str, str]:
            self.created.append(values)
            return {"uid": f"wrk-probe-{len(self.created)}"}

        def state(self, uid: str) -> dict[str, object]:
            state = super().state(uid)
            if uid == "wrk-probe-1":
                state["urls"] = [
                    {
                        "port": 8080,
                        "url": "https://source-review-mock.example",
                    }
                ]
            return state

        def logs(self, uid: str, *, tail: int) -> str:
            assert tail > 0
            if uid == "wrk-probe-1":
                return (
                    'SOURCE_REVIEW_COMPLETE={"categories": ["none"], '
                    '"clearance_certified": true, "ok": true, '
                    '"prompt_revision": "l2-kimi-source-review-v33", '
                    '"risk_level": "low"}'
                )
            return "layered source review kimi-k3 gpt-5.6-sol"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return (
                b'{"ok": true, "artifact_sha256": "' + (b"a" * 64) + b'"}'
            )

    client = LiveSourceReviewTargon()
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    monkeypatch.setattr(
        "screener_capacity.targon_cli.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(
        "screener_capacity.targon_cli.time.sleep", lambda _seconds: None
    )
    monkeypatch.setattr(
        "screener_capacity.targon_cli._load_source_review_api_key",
        lambda: "probe-live-openrouter-key",
    )
    args = Namespace(
        resource="cpu-small",
        image="registry.example/screener@sha256:" + "a" * 64,
        provision_timeout_seconds=1,
        review_timeout_seconds=1800,
        keep=False,
        starter_kit=True,
        live_model=True,
    )

    assert command_source_review_probe(args) == 0

    env = {row["name"]: row["value"] for row in client.created[1]["envs"]}
    assert env["SCREENER_SOURCE_REVIEW_API_KEY"] == "probe-live-openrouter-key"
    assert env["SCREENER_SOURCE_REVIEW_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert env["SCREENER_L2_REVIEW_MODE"] == "enforce"
    assert env["SCREENER_L2_REVIEW_MODEL"] == "moonshotai/kimi-k3"
    assert env["SCREENER_L3_REVIEW_ENABLED"] == "true"
    assert env["SCREENER_L3_REVIEW_MODEL"] == "openai/gpt-5.6-sol"
    assert env["SCREENER_L2_ALWAYS_ESCALATE"] == "true"
    assert env["SCREENER_L2_MAX_COMPLETION_TOKENS"] == "8192"
    output = capsys.readouterr().out
    assert '"capability": "AVAILABLE"' in output
    assert "l2-kimi-source-review-v33" in output
    assert "kimi-k3" in output


def test_starter_kit_source_review_mock_script_compiles() -> None:
    script = _source_review_starter_kit_mock_script(
        review_id="550e8400-e29b-41d4-a716-446655440000",
        job_token="job-" + "x" * 48,
        archive_url="https://example.invalid/starter.tgz",
    )
    compile(script, "<starter-kit-source-review-mock>", "exec")
    assert "SOURCE_REVIEW_COMPLETE=" in script
    assert "/chat/completions" not in script


def test_source_review_probe_fixture_and_mock_script_are_self_contained() -> None:
    archive = _source_review_probe_archive()
    script = _source_review_mock_script(
        review_id="550e8400-e29b-41d4-a716-446655440000",
        job_token="job-" + "x" * 48,
        artifact=archive,
    )

    compile(script, "<source-review-mock>", "exec")
    assert archive.startswith(b"\x1f\x8b")
    assert "SOURCE_REVIEW_COMPLETE=" in script


def test_logs_command_prints_redacted_tail(monkeypatch, capsys) -> None:
    client = _Targon()

    def logs(uid: str, *, tail: int) -> str:
        assert uid == "wrk-by6akuyvjqyd"
        assert tail == 12
        return "kaniko ok\nAuthorization: Bearer secret-token\n"

    monkeypatch.setattr(client, "logs", logs)
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    args = Namespace(uid="wrk-by6akuyvjqyd", tail=12, include_state=False)

    assert command_logs(args) == 0

    output = capsys.readouterr().out
    assert "kaniko ok" in output
    assert "secret-token" not in output
    assert "[redacted]" in output


def test_logs_command_can_include_safe_state(monkeypatch, capsys) -> None:
    client = _Targon()
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    args = Namespace(uid="wrk-by6akuyvjqyd", tail=8, include_state=True)

    assert command_logs(args) == 0

    output = capsys.readouterr().out
    assert '"uid": "wrk-by6akuyvjqyd"' in output
    assert "---LOGS---" in output
    assert "AGENT_RUNTIME_AVAILABLE" in output


def test_state_command_rejects_invalid_uid() -> None:
    try:
        command_state(Namespace(uid="not-a-workload"))
    except SystemExit as error:
        assert "workload uid" in str(error)
    else:
        raise AssertionError("expected invalid uid to fail closed")


def test_logs_command_explains_404_after_replica_exits(monkeypatch) -> None:
    client = _Targon()

    def logs(_uid: str, *, tail: int) -> str:
        del tail
        raise TargonAPIError(
            operation="GET /workloads/wrk-by6akuyvjqyd/logs",
            status=404,
            reason="HTTP error",
        )

    monkeypatch.setattr(client, "logs", logs)
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )
    args = Namespace(uid="wrk-by6akuyvjqyd", tail=12, include_state=False)

    try:
        command_logs(args)
    except TargonAPIError as error:
        assert error.status == 404
        assert "left running" in error.reason
    else:
        raise AssertionError("expected 404 logs to fail closed")
