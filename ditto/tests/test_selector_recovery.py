"""Focused parser, interrupted SQLite state and local observation recovery controls.

These run without root, installed services, chain access or signer credentials.
The separate-UID spool controls remain in test_selector_handoff.py.
"""

import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto.tests.test_activity_observer import fixture
from ditto.treasury.activity_export import write_selector_snapshot
from ditto.treasury.activity_observer import ActivityQueue
from ditto.treasury.selector_handoff import SelectorHandoff


def handoff():
    config, _ = fixture()
    return SelectorHandoff(
        "/absent/outbox",
        "/absent/acks",
        10001,
        10002,
        10003,
        config.approval.policy.collector_policy_digest,
    )


@pytest.mark.parametrize("value", [None, True, 1, [], "invalid", {}])
@pytest.mark.parametrize("script", ["publisher", "observer"])
def test_handoff_cli_invalid_section_is_concise_before_io(
    tmp_path, monkeypatch, capsys, value, script
):
    from scripts import treasury_activity_observer, treasury_selector_publisher

    cli = (
        treasury_selector_publisher
        if script == "publisher"
        else treasury_activity_observer
    )
    raw = json.dumps({"enabled": True, "selector_handoff": value}).encode()
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    args = [
        "cli",
        "--config",
        str(path),
        "--config-sha256",
        hashlib.sha256(raw).hexdigest(),
    ]
    if script == "publisher":
        args += [
            "--snapshot",
            "/absent/snapshot",
            "--state",
            "/absent/state",
            "--policy",
            "/absent/policy",
            "--policy-sha256",
            "a" * 64,
        ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "selector_handoff" in stderr
    assert "Traceback" not in stderr


def test_enabled_publisher_missing_handoff_is_concise(tmp_path, monkeypatch, capsys):
    from scripts import treasury_selector_publisher as cli

    raw = b'{"enabled":true}'
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli",
            "--config",
            str(path),
            "--config-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--snapshot",
            "/absent/snapshot",
            "--state",
            "/absent/state",
            "--policy",
            "/absent/policy",
            "--policy-sha256",
            "a" * 64,
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "selector_handoff must be an object" in capsys.readouterr().err


def test_future_fields_have_no_authority_or_digest_effect_and_known_bounds_are_strict():
    expected = handoff()
    body = {**asdict(expected), "future_schema": {"value": 1}, "maxPendngPages": 1}
    parsed = SelectorHandoff.from_mapping(body)
    assert parsed == expected and parsed.digest == expected.digest
    assert parsed.max_pending_pages == 10
    assert "maxPendngPages" not in asdict(parsed)
    for bound in (True, 0, 11, "2"):
        with pytest.raises(ValueError, match="bounded"):
            SelectorHandoff.from_mapping({**body, "max_pending_pages": bound})


def test_observer_future_fields_remain_compatible_with_disabled_no_io(
    tmp_path, monkeypatch, capsys
):
    from scripts import treasury_activity_observer as cli

    config, _ = fixture()
    body = {
        "approval": config.approval.model_dump(mode="json"),
        "settings_checksum": config.settings_checksum,
        "start_block": config.start_block,
        "selector_handoff": {**asdict(handoff()), "future_field": True},
    }
    raw = json.dumps(body).encode()
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli",
            "--config",
            str(path),
            "--config-sha256",
            hashlib.sha256(raw).hexdigest(),
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out) == {
        "status": "disabled",
        "authority": "none",
    }


@pytest.mark.parametrize(
    "lost",
    [
        "all",
        "pin",
        "cursor",
        "journal_cursor",
        "block_progress",
        "pending",
        "selector_pages",
    ],
)
def test_interrupted_or_lost_handoff_schema_requires_recovery_without_recreating(lost):
    config, _ = fixture()
    config = replace(config, selector_handoff=handoff())
    queue = ActivityQueue.__new__(ActivityQueue)
    queue.db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        if lost != "all":
            queue._initialize(config)
            queue.db.execute(f"DROP TABLE {lost}")
        before = queue.db.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        with pytest.raises(ValueError, match="recovery required"):
            queue._initialize(config, existing_handoff=True)
        assert (
            queue.db.execute(
                "SELECT name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            == before
        )
    finally:
        queue.db.close()


def test_malformed_existing_handoff_schema_requires_recovery():
    config, _ = fixture()
    config = replace(config, selector_handoff=handoff())
    queue = ActivityQueue.__new__(ActivityQueue)
    queue.db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        queue._initialize(config)
        queue.db.executescript(
            "DROP TABLE selector_pages; CREATE TABLE selector_pages(wrong TEXT);"
        )
        with pytest.raises(ValueError, match="recovery required"):
            queue._initialize(config, existing_handoff=True)
        assert (
            queue.db.execute("PRAGMA table_info(selector_pages)").fetchone()[1]
            == "wrong"
        )
    finally:
        queue.db.close()


def test_snapshot_preserves_actual_sqlite_automatic_rollback_error(tmp_path):
    class AutoRollback(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "SELECT digest,role FROM pin":
                # Real RAISE(ROLLBACK) ends the active source transaction before
                # the snapshot handler runs; an unconditional rollback masks it.
                return super().execute("INSERT INTO aborts VALUES(1)")
            return super().execute(sql, *args)

    tmp_path.chmod(0o700)
    source = sqlite3.connect(tmp_path / "journal.sqlite", factory=AutoRollback)
    source.executescript(
        "CREATE TABLE pin(digest,role); CREATE TABLE aborts(value);"
        "CREATE TRIGGER abort_source BEFORE INSERT ON aborts BEGIN "
        "SELECT RAISE(ROLLBACK,'original source failure'); END;"
    )
    snapshot = tmp_path / "snapshot.sqlite"
    snapshot.write_bytes(b"prior valid snapshot sentinel")
    snapshot.chmod(0o600)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="original source failure"):
            write_selector_snapshot(source, snapshot, SimpleNamespace(digest="a" * 64))
        assert not source.in_transaction
        assert snapshot.read_bytes() == b"prior valid snapshot sentinel"
        assert source.execute("SELECT count(*) FROM aborts").fetchone() == (0,)
        assert not list(tmp_path.glob(".selector-snapshot-*"))
    finally:
        source.close()


def test_disabled_signer_local_snapshot_recovery_never_calls_chain_or_tick(
    tmp_path, monkeypatch, capsys
):
    from scripts import treasury_collector as cli

    tmp_path.chmod(0o700)
    policy = SimpleNamespace(digest="a" * 64, enabled=False)
    journal_path = tmp_path / "journal.sqlite"
    with sqlite3.connect(journal_path) as db:
        db.executescript(
            "CREATE TABLE pin(digest,role); CREATE TABLE operations("
            "id INTEGER PRIMARY KEY,role,state,source_block,bucket,amount,"
            "settlement_json);"
        )
        db.execute("INSERT INTO pin VALUES(?,'transfer')", (policy.digest,))
    snapshot = tmp_path / "snapshot.sqlite"
    opens = []

    class Journal:
        def __init__(self, path, *_args):
            opens.append(path)
            self.db = sqlite3.connect(path)

        def close(self):
            self.db.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "disabled observation recovery cannot call chain or money tick"
        )

    monkeypatch.setattr(cli, "load_policy", lambda *_args: policy)
    monkeypatch.setattr(cli, "CollectorJournal", Journal)
    monkeypatch.setattr(cli, "tick", forbidden)
    monkeypatch.setitem(sys.modules, "bittensor", SimpleNamespace(Subtensor=forbidden))
    args = [
        "cli",
        "--role",
        "transfer",
        "--policy",
        "/absent/policy",
        "--policy-sha256",
        policy.digest,
        "--journal",
        str(journal_path),
        "--selector-snapshot",
        str(snapshot),
    ]
    monkeypatch.setattr(sys, "argv", args)
    cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert opens == [] and not snapshot.exists()
    monkeypatch.setattr(sys, "argv", [*args, "--snapshot-only"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "selector_snapshot_ready"
    assert opens == [journal_path]
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT * FROM snapshot_meta").fetchone() == (1, 0, 0)
    assert not any(
        Path(str(snapshot) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    )


@pytest.mark.parametrize(
    "original_enabled,role",
    [(False, "transfer"), (True, "transfer"), (False, "registration")],
)
def test_snapshot_only_real_journal_requires_unchanged_historical_pin(
    tmp_path, monkeypatch, capsys, original_enabled, role
):
    from ditto.tests.test_collector_automation import policy
    from ditto.treasury.collector import CollectorJournal
    from scripts import treasury_collector as cli

    tmp_path.chmod(0o700)
    original = policy(enabled=original_enabled)
    approved_disabled = policy(enabled=False)
    journal_path = tmp_path / "journal.sqlite"
    CollectorJournal(journal_path, original, role, initialize=True).close()
    snapshot = tmp_path / "snapshot.sqlite"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshot-only must not load chain, keys or tick")

    monkeypatch.setattr(cli, "load_policy", lambda *_args: approved_disabled)
    monkeypatch.setattr(cli, "tick", forbidden)
    monkeypatch.setitem(sys.modules, "bittensor", SimpleNamespace(Subtensor=forbidden))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli",
            "--role",
            "transfer",
            "--policy",
            "/absent/policy",
            "--policy-sha256",
            approved_disabled.digest,
            "--journal",
            str(journal_path),
            "--snapshot-only",
            "--selector-snapshot",
            str(snapshot),
        ],
    )
    if original_enabled or role != "transfer":
        with pytest.raises(ValueError, match="pin unavailable or changed"):
            cli.main()
        assert not snapshot.exists()
        assert capsys.readouterr().out == ""
    else:
        cli.main()
        assert (
            json.loads(capsys.readouterr().out)["status"] == "selector_snapshot_ready"
        )
        with sqlite3.connect(snapshot) as db:
            assert db.execute("SELECT digest,role FROM pin").fetchone() == (
                approved_disabled.digest,
                "transfer",
            )
    with sqlite3.connect(journal_path) as db:
        assert db.execute("SELECT digest,role FROM pin").fetchone() == (
            original.digest,
            role,
        )
        assert db.execute("SELECT count(*) FROM operations").fetchone() == (0,)


def test_staged_unit_requires_operator_owned_existing_directories():
    root = Path(__file__).resolve().parents[2]
    unit = (
        root / "infra/systemd/sn118-treasury-selector-publisher.service"
    ).read_text()
    for name in ("outbox", "acknowledgments"):
        assert f"AssertPathIsDirectory=/var/lib/sn118-selector-handoff/{name}" in unit
    assert "ExecStartPre=" not in unit
    assert "ProtectSystem=strict" in unit
    with pytest.raises(FileNotFoundError):
        handoff().directories()


@pytest.mark.parametrize(
    "schema", ["absent_metadata", "malformed_metadata", "absent_operations", "complete"]
)
def test_readonly_snapshot_metadata_refusal_preserves_source_and_other_sql_errors(
    tmp_path, schema
):
    from ditto.treasury.activity_export import export_snapshot_distributions

    tmp_path.chmod(0o700)
    path = tmp_path / "snapshot.sqlite"
    policy = SimpleNamespace(digest="a" * 64)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE pin(digest,role)")
        db.execute("INSERT INTO pin VALUES(?,'transfer')", (policy.digest,))
        if schema == "malformed_metadata":
            db.execute("CREATE TABLE snapshot_meta(wrong_column)")
        elif schema != "absent_metadata":
            db.execute("CREATE TABLE snapshot_meta(version,rows,last_operation)")
            db.execute("INSERT INTO snapshot_meta VALUES(1,0,0)")
        if schema != "absent_operations":
            db.execute(
                "CREATE TABLE operations(id,role,state,source_block,bucket,amount,"
                "settlement_json)"
            )
    path.chmod(0o600)
    before = path.read_bytes()
    seen = []

    def epoch_at(_block):
        raise AssertionError("no source row requires chain read")

    def export():
        return export_snapshot_distributions(
            path,
            policy,
            epoch_at,
            after_id=0,
            minimum=(0, 0),
            validate_history=lambda _db: seen.append("validated"),
        )

    if schema == "absent_metadata":
        with pytest.raises(
            ValueError, match="snapshot history lost, rolled back or changed"
        ):
            export()
        assert seen == []
    elif schema in {"malformed_metadata", "absent_operations"}:
        with pytest.raises(sqlite3.OperationalError):
            export()
        assert seen == []
    else:
        assert export() == ([], (0, 0))
        assert seen == ["validated"]
    assert path.read_bytes() == before
    assert not any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    )


def test_publisher_signed_policy_mismatch_is_argparse_refusal_before_io(
    tmp_path, monkeypatch, capsys
):
    from scripts import treasury_selector_publisher as cli

    raw = json.dumps({"enabled": True, "selector_handoff": asdict(handoff())}).encode()
    path = tmp_path / "config.json"
    path.write_bytes(raw)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("policy mismatch cannot open state or network")

    monkeypatch.setattr(
        cli, "load_policy", lambda *_args: SimpleNamespace(digest="b" * 64)
    )
    monkeypatch.setattr(cli, "SelectorPublisher", forbidden)
    monkeypatch.setattr(cli, "PublicEpochReader", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cli",
            "--config",
            str(path),
            "--config-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--snapshot",
            "/absent/snapshot",
            "--state",
            "/absent/state",
            "--policy",
            "/absent/policy",
            "--policy-sha256",
            "b" * 64,
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "selector exporter signed collector policy differs" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("failure_site", ["tick", "epoch"])
@pytest.mark.parametrize("failure_kind", ["runtime", "state", "auth", "protocol"])
def test_actual_reader_cleanup_preserves_terminal_publisher_failure_without_retry(
    failure_site, failure_kind
):
    from websockets.exceptions import ConnectionClosedError, InvalidHandshake
    from websockets.frames import Close

    from ditto.treasury.selector_handoff import run_publisher
    from scripts.treasury_selector_publisher import PublicEpochReader

    original = {
        "runtime": ValueError("original runtime policy failure"),
        "state": sqlite3.OperationalError("original publisher state failure"),
        "auth": InvalidHandshake("original authentication failure"),
        "protocol": ConnectionClosedError(
            Close(1008, "original protocol refusal"), None
        ),
    }[failure_kind]
    calls = []

    def fail(*_args):
        calls.append(failure_site)
        raise original

    def close():
        calls.append("close")
        raise RuntimeError("unrelated teardown failure")

    reader = PublicEpochReader.__new__(PublicEpochReader)
    reader.subtensor = SimpleNamespace(
        substrate=SimpleNamespace(get_block_hash=fail), close=close
    )

    class Publisher:
        def recover(self):
            pass

        def tick(self, _path, epoch_at):
            if failure_site == "tick":
                return fail()
            return epoch_at(120)

    def factory():
        calls.append("connect")
        return reader

    def forbidden_sleep(_seconds):
        raise AssertionError("terminal failure must not back off/retry")

    with pytest.raises(type(original)) as error:
        run_publisher(Publisher(), None, factory, sleep=forbidden_sleep)
    assert error.value is original
    assert calls == ["connect", failure_site, "close"]
