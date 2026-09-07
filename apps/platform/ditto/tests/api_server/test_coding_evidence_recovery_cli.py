from pathlib import Path

import pytest

from ditto import coding_evidence_recovery as cli


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--config", "/unread/config.json"],
        ["--resume-reserved", "--config", "/unread/config.json"],
        ["--resume-reserved", "--config", "/unread/config.json", "--confirm", "yes"],
        ["--inspect", "--config", "/unread/config.json", "--confirm", cli.CONFIRMATION],
    ],
)
def test_cli_refuses_before_loading_any_configuration(arguments, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("invalid CLI reached configuration")

    monkeypatch.setattr(cli, "run", forbidden)
    monkeypatch.setattr("sys.argv", ["coding-evidence-recovery", *arguments])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_cli_failure_is_redacted(monkeypatch, capsys):
    async def unavailable(*_args, **_kwargs):
        raise ValueError("private-path-and-secret-must-not-leak")

    monkeypatch.setattr(cli, "run", unavailable)
    monkeypatch.setattr("sys.argv", ["recovery", "--inspect", "--config", "/private"])
    assert cli.main() == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "native evidence recovery unavailable\n"


async def test_inspection_does_not_load_hippius_credentials(monkeypatch):
    from ditto.api_server import coding_hosted_runtime_io as runtime_io
    from ditto.api_server.coding_evidence_recovery import HostedEvidenceRecovery
    from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceSpool
    from ditto.db import factory

    paths = []

    def read(path, *_args):
        paths.append(path)
        if path == Path("/protected/config.json"):
            return {
                "schema": "dittobench-coding-evidence-recovery-config-v2",
                "shadow_only": True,
                "weight_eligible": False,
                "target": {
                    "phase": "authoring",
                    "worker_id": "00000000-0000-0000-0000-000000000001",
                    "evaluation_id": "00000000-0000-0000-0000-000000000002",
                    "attempt_id": "00000000-0000-0000-0000-000000000003",
                    "identity_sha256": "a" * 64,
                },
                "postgres_environment_file": "/protected/postgres.json",
                "spool_root": "/protected/spool",
            }
        assert path == Path("/protected/postgres.json")
        return [
            "POSTGRES_HOST=synthetic",
            "POSTGRES_PORT=5432",
            "POSTGRES_USER=synthetic",
            "POSTGRES_PASSWORD=synthetic",
            "POSTGRES_DB=synthetic",
        ]

    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    closed = []

    def initialize(self, _root, **kwargs):
        assert kwargs["read_only"] is True
        self._read_only = True

    async def inspect(self, _target):
        assert self._config is None and self._probe_path is None
        return {"state": "prepared"}

    monkeypatch.setattr(runtime_io, "read_json", read)
    monkeypatch.setattr(factory, "create_db_engine", lambda _config: engine)
    monkeypatch.setattr(factory, "create_session_maker", lambda _engine: None)
    monkeypatch.setattr(HostedEvidenceSpool, "__init__", initialize)
    monkeypatch.setattr(HostedEvidenceSpool, "close", lambda _self: closed.append(True))
    monkeypatch.setattr(HostedEvidenceRecovery, "inspect", inspect)
    assert await cli.run(Path("/protected/config.json"), publish=False) == {
        "state": "prepared"
    }
    assert paths == [Path("/protected/config.json"), Path("/protected/postgres.json")]
    assert engine.disposed and closed == [True]
