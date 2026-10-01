"""Real separate POSIX users, durable delivery recovery and bounded pagination.

Root-only controls run inside a disposable Linux QA container, never install
host users or permissions. Other platforms still run schema/default-off tests.
SQLite uses its actual FULL/WAL mode; crash tests inject boundaries, not power loss.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ditto.tests.test_activity_observer import MCP, Chain, fixture, h
from ditto.treasury.activity_export import write_selector_snapshot
from ditto.treasury.activity_observer import (
    ActivityQueue,
    ObservationUnavailable,
    observer_tick,
)
from ditto.treasury.collector import canonical
from ditto.treasury.selector_handoff import (
    MAX_BYTES,
    SelectorChainUnavailable,
    SelectorHandoff,
    SelectorPublisher,
    digest,
    import_pages,
    public_paths,
    read_public,
    run_publisher,
    selection,
    validate_page,
    write_public,
)

PUBLISHER, OBSERVER, GROUP = 10001, 10002, 10003


def as_user(uid, function):
    """Fork without changing the test runner's identity; bounded JSON result."""
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        try:
            os.setgroups([GROUP])
            os.setgid(GROUP)
            os.setuid(uid)
            result = {"ok": function()}
        except BaseException:
            result = {"error": traceback.format_exc()}
        raw = json.dumps(result).encode()
        if len(raw) > 60000:
            raw = b'{"error":"child result exceeds bound"}'
        with os.fdopen(writer, "wb") as output:
            output.write(raw)
        os._exit(0)
    os.close(writer)
    with os.fdopen(reader, "rb") as source:
        raw = source.read(60001)
    _, status = os.waitpid(child, 0)
    assert status == 0 and len(raw) <= 60000
    body = json.loads(raw)
    assert "error" not in body, body.get("error")
    return body["ok"]


@pytest.fixture
def spool(tmp_path):
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("actual cross-user spool requires disposable root Linux container")
    # Only ephemeral test ancestors inside the container, no host users/dirs.
    for path in (tmp_path, tmp_path.parent, tmp_path.parent.parent):
        path.chmod(0o755)
    config, settings = fixture()
    handoff = SelectorHandoff(
        str(tmp_path / "outbox"),
        str(tmp_path / "acks"),
        PUBLISHER,
        OBSERVER,
        GROUP,
        config.approval.policy.collector_policy_digest,
        max_pending_pages=2,
    )
    for name, owner, mode in (
        ("outbox", PUBLISHER, 0o750),
        ("acks", OBSERVER, 0o750),
        ("publisher", PUBLISHER, 0o700),
        ("observer", OBSERVER, 0o700),
    ):
        path = tmp_path / name
        path.mkdir(mode=mode)
        os.chown(path, owner, GROUP)
    journal = tmp_path / "publisher/journal.sqlite"
    with sqlite3.connect(journal) as db:
        db.executescript(
            "CREATE TABLE pin(digest,role); CREATE TABLE operations("
            "id INTEGER PRIMARY KEY,role,state,source_block,bucket,amount,"
            "settlement_json,signed_json,call_json);"
        )
        db.execute(
            "INSERT INTO pin VALUES(?,'transfer')", (handoff.collector_policy_digest,)
        )
    journal.chmod(0o600)
    os.chown(journal, PUBLISHER, GROUP)
    return tmp_path, handoff, replace(config, selector_handoff=handoff), settings


def add_rows(spool, count, *, start=1, state="finalized"):
    root, _, _, _ = spool
    with sqlite3.connect(root / "publisher/journal.sqlite") as db:
        for i in range(start, start + count):
            db.execute(
                "INSERT INTO operations VALUES(?,'transfer',?,?,'gamma',25,?,?,?)",
                (
                    i,
                    state,
                    120 + i,
                    canonical(
                        {
                            "status": "finalized",
                            "block": 130 + i,
                            "block_hash": h(130 + i),
                            "extrinsic_index": 0,
                            "extrinsic_hash": h(i),
                        }
                    ),
                    "PRIVATE_SIGNED_PAYLOAD",
                    "PRIVATE_CALL_PAYLOAD",
                ),
            )


def refresh_snapshot(spool):
    root, handoff, _, _ = spool

    def snapshot():
        with sqlite3.connect(root / "publisher/journal.sqlite") as db:
            write_selector_snapshot(
                db,
                root / "publisher/selector-snapshot.db",
                type("Policy", (), {"digest": handoff.collector_policy_digest})(),
            )

    if os.geteuid() == PUBLISHER:
        return snapshot()
    return as_user(PUBLISHER, snapshot)


def publish(spool, *, initialize=False):
    root, handoff, _, _ = spool
    refresh_snapshot(spool)

    def tick():
        publisher = SelectorPublisher(
            root / "publisher/state.sqlite", handoff, initialize=initialize
        )
        try:
            return publisher.tick(root / "publisher/selector-snapshot.db", lambda _: 9)
        finally:
            publisher.close()

    return as_user(PUBLISHER, tick)


def observe(
    spool, *, initialize=False, unknown_at=None, bad_ack=False, deliveries=None
):
    root, _, config, settings = spool
    if deliveries is not None:
        config = replace(config, max_deliveries=deliveries)

    def tick():
        class Uncertain(MCP):
            def call(self, name, arguments):
                if name == "record_treasury_receipt" and len(self.calls) == unknown_at:
                    self.unknown_once = True
                return super().call(name, arguments)

        chain = Chain(config)
        chain.height = 119
        mcp = Uncertain(config, settings)
        mcp.bad_ack = bad_ack
        try:
            result = observer_tick(
                config,
                state_path=root / "observer/queue.sqlite",
                transfer_journal=None,
                chain=chain,
                mcp=mcp,
                initialize_selector_state=initialize,
            )
        except (ObservationUnavailable, ValueError) as exc:
            result = {"error": type(exc).__name__}
        return {**result, "calls": len(mcp.calls), "accepted": len(mcp.records)}

    return as_user(OBSERVER, tick)


def test_more_than_100_pagination_backpressure_restart_and_no_private_payloads(spool):
    root, handoff, _, _ = spool
    add_rows(spool, 251)
    assert publish(spool, initialize=True)["count"] == 100
    assert publish(spool)["count"] == 100
    assert publish(spool)["status"] == "backpressure"
    with sqlite3.connect(root / "publisher/state.sqlite") as db:
        assert db.execute("SELECT operation FROM cursor").fetchone() == (200,)
    pages = public_paths(handoff.outbox)
    assert len(pages) == 2
    assert all("PRIVATE_" not in p.read_text() for p in pages)
    result = observe(spool, initialize=True)
    assert result["delivered"] == 100 and result["pending"] == 100
    assert len(public_paths(handoff.acknowledgments)) == 1
    assert observe(spool)["delivered"] == 100
    assert publish(spool)["count"] == 51
    assert observe(spool)["delivered"] == 51
    assert publish(spool)["status"] == "waiting"
    assert observe(spool)["calls"] == 0
    assert public_paths(handoff.outbox) == public_paths(handoff.acknowledgments) == []
    with sqlite3.connect(root / "observer/queue.sqlite") as db:
        assert db.execute(
            "SELECT count(*) FROM pending WHERE receipt_id IS NOT NULL"
        ).fetchone() == (251,)
    with sqlite3.connect(root / "publisher/state.sqlite") as db:
        assert db.execute("SELECT operation FROM cursor").fetchone() == (251,)
        assert db.execute(
            "SELECT count(*) FROM pages WHERE ack IS NOT NULL AND retired=1"
        ).fetchone() == (3,)


def test_partial_unknown_delivery_retains_page_and_receipts_until_replay(spool):
    root, handoff, _, _ = spool
    add_rows(spool, 3)
    publish(spool, initialize=True)
    result = observe(spool, initialize=True, unknown_at=1)
    assert result["error"] == "ObservationUnavailable" and result["accepted"] == 2
    assert public_paths(handoff.acknowledgments) == []
    with sqlite3.connect(root / "observer/queue.sqlite") as db:
        assert db.execute(
            "SELECT count(*) FROM pending WHERE receipt_id IS NOT NULL"
        ).fetchone() == (1,)
    # Unknown accepted delivery replays; first durably accepted item is not sent.
    assert observe(spool)["calls"] == 2
    assert len(public_paths(handoff.acknowledgments)) == 1
    assert publish(spool)["status"] == "waiting"
    assert observe(spool)["calls"] == 0


def test_http_result_with_wrong_coordinates_cannot_ack_page(spool):
    _, handoff, _, _ = spool
    add_rows(spool, 2)
    publish(spool, initialize=True)
    assert observe(spool, initialize=True, bad_ack=True)["error"] == "ValueError"
    assert public_paths(handoff.acknowledgments) == []
    assert observe(spool)["delivered"] == 2


def test_unresolved_lower_operation_barrier_and_terminal_failed_expired(spool):
    root, _, _, _ = spool
    add_rows(spool, 1, state="signed")
    add_rows(spool, 1, start=2, state="failed")
    add_rows(spool, 1, start=3, state="expired")
    add_rows(spool, 2, start=4)
    assert publish(spool, initialize=True)["status"] == "waiting"
    with sqlite3.connect(root / "publisher/journal.sqlite") as db:
        db.execute("UPDATE operations SET state='finalized' WHERE id=1")
    assert publish(spool)["count"] == 3
    assert observe(spool, initialize=True)["delivered"] == 3
    assert publish(spool)["status"] == "waiting"
    # A nonterminal row behind the durable cursor refuses instead of losing a
    # late reconciliation. Explicit history repair, never cursor reset, needed.
    with sqlite3.connect(root / "publisher/journal.sqlite") as db:
        db.execute("UPDATE operations SET state='signed' WHERE id=2")

    def refused():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", spool[1])
        try:
            with pytest.raises(ValueError, match="ordering"):
                publisher.tick(root / "publisher/selector-snapshot.db", lambda _: 9)
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, refused)


def test_committed_page_and_cursor_before_publish_reconstructs(spool, monkeypatch):
    import ditto.treasury.selector_handoff as module

    root, handoff, _, _ = spool
    add_rows(spool, 2)
    actual = module.write_public
    monkeypatch.setattr(
        module,
        "write_public",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )

    def interrupted():
        publisher = SelectorPublisher(
            root / "publisher/state.sqlite", handoff, initialize=True
        )
        try:
            with pytest.raises(RuntimeError, match="injected"):
                publisher.tick(root / "publisher/selector-snapshot.db", lambda _: 9)
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, interrupted)
    assert not public_paths(handoff.outbox)
    with sqlite3.connect(root / "publisher/state.sqlite") as db:
        assert db.execute("SELECT operation FROM cursor").fetchone() == (2,)
    monkeypatch.setattr(module, "write_public", actual)
    assert publish(spool)["status"] == "waiting"
    assert len(public_paths(handoff.outbox)) == 1
    # Public page can disappear: retained authoritative page reconstructs it.
    public_paths(handoff.outbox)[0].unlink()
    assert publish(spool)["status"] == "waiting"
    assert observe(spool, initialize=True)["delivered"] == 2


def test_ack_durable_before_prune_crash_recovers(spool, monkeypatch):
    root, handoff, _, _ = spool
    add_rows(spool, 2)
    publish(spool, initialize=True)
    observe(spool, initialize=True)
    original = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path.parent == Path(handoff.outbox):
            raise RuntimeError("prune crash")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    def interrupted():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", handoff)
        try:
            with pytest.raises(RuntimeError, match="prune crash"):
                publisher.recover()
            assert publisher.db.execute(
                "SELECT acked,retired FROM pages"
            ).fetchone() == (1, 0)
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, interrupted)
    monkeypatch.setattr(Path, "unlink", original)
    assert publish(spool)["status"] == "waiting"
    assert observe(spool)["calls"] == 0


def test_observer_ack_written_before_cleanup_crash_is_replayed_exactly(spool):
    _, handoff, _, _ = spool
    add_rows(spool, 1)
    publish(spool, initialize=True)
    observe(spool, initialize=True)
    ack = public_paths(handoff.acknowledgments)[0].read_bytes()
    assert observe(spool)["calls"] == 0
    assert public_paths(handoff.acknowledgments)[0].read_bytes() == ack
    assert publish(spool)["status"] == "waiting"


@pytest.mark.parametrize("fault", ["unknown", "receipt_changed"])
def test_observer_does_not_erase_conflicting_retired_ack(spool, fault):
    _, handoff, _, _ = spool
    add_rows(spool, 1)
    publish(spool, initialize=True)
    observe(spool, initialize=True)
    publish(spool)
    path = public_paths(handoff.acknowledgments)[0]
    ack = json.loads(path.read_text())
    if fault == "unknown":
        path.rename(path.with_name("a" * 64 + ".json"))
    else:
        ack["receipts"][0]["receipt"] = "a" * 64
        path.write_text(canonical(ack))
    assert observe(spool)["error"] == "ValueError"
    assert len(public_paths(handoff.acknowledgments)) == 1


@pytest.mark.parametrize(
    "fault", ["policy", "selection", "operation", "partial", "unknown", "reused"]
)
def test_wrong_partial_unknown_and_reused_ack_refuse(spool, fault):
    root, handoff, _, _ = spool
    add_rows(spool, 2)
    publish(spool, initialize=True)
    observe(spool, initialize=True)
    path = public_paths(handoff.acknowledgments)[0]
    ack = json.loads(path.read_text())
    if fault == "policy":
        ack["policy"] = "a" * 64
    elif fault == "selection":
        ack["receipts"][0]["selection"] = "a" * 64
    elif fault == "operation":
        ack["receipts"][0]["operation"] = 99
    elif fault == "partial":
        ack["receipts"].pop()
    elif fault == "unknown":
        path = path.with_name("a" * 64 + ".json")
    else:
        publish(spool)
        ack["receipts"][0]["receipt"] = "b" * 64
    path.write_text(canonical(ack))
    path.chmod(0o640)
    os.chown(path, OBSERVER, GROUP)

    def refused():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", handoff)
        try:
            with pytest.raises(ValueError, match="acknowledgment"):
                publisher.recover()
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, refused)


def test_state_loss_or_pin_change_no_auto_initialize(spool):
    root, handoff, config, _ = spool

    def missing_publisher():
        with pytest.raises(FileNotFoundError):
            SelectorPublisher(root / "publisher/state.sqlite", handoff)

    as_user(PUBLISHER, missing_publisher)

    def missing_observer():
        with pytest.raises(FileNotFoundError):
            ActivityQueue(root / "observer/queue.sqlite", config)

    as_user(OBSERVER, missing_observer)
    add_rows(spool, 1)
    publish(spool, initialize=True)
    observe(spool, initialize=True)

    def changed():
        with pytest.raises(ValueError, match="changed"):
            SelectorPublisher(
                root / "publisher/state.sqlite", replace(handoff, max_pending_pages=1)
            )

    as_user(PUBLISHER, changed)

    def changed_observer():
        with pytest.raises(ValueError, match="changed"):
            ActivityQueue(
                root / "observer/queue.sqlite", replace(config, start_block=121)
            )

    as_user(OBSERVER, changed_observer)
    # Published page without retained private page/cursor cannot be adopted.
    with sqlite3.connect(root / "publisher/state.sqlite") as db:
        db.execute("DELETE FROM pages")

    def lost():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", handoff)
        try:
            with pytest.raises(ValueError, match="cursor/page loss"):
                publisher.recover()
        finally:
            publisher.close()

    as_user(PUBLISHER, lost)


def test_visible_page_without_committed_cursor_cannot_advance(spool):
    root, handoff, _, _ = spool
    add_rows(spool, 1)
    publish(spool, initialize=True)
    # Production commits cursor+retained page before publication. Simulate the
    # opposite boundary/stale restored state: no adoption from public data alone.
    with sqlite3.connect(root / "publisher/state.sqlite") as db:
        db.execute("UPDATE cursor SET operation=0")

    def refused():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", handoff)
        try:
            with pytest.raises(ValueError, match="cursor/page loss"):
                publisher.recover()
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, refused)


def test_missing_legacy_selector_halts_without_reading_signed_payload(spool):
    root, handoff, _, _ = spool
    add_rows(spool, 1)
    with sqlite3.connect(root / "publisher/journal.sqlite") as db:
        db.execute(
            "UPDATE operations SET settlement_json=?",
            (canonical({"status": "finalized"}),),
        )

    def refused():
        publisher = SelectorPublisher(
            root / "publisher/state.sqlite", handoff, initialize=True
        )
        try:
            with pytest.raises(ValueError, match="selector missing"):
                publisher.tick(root / "publisher/selector-snapshot.db", lambda _: 9)
            assert publisher.db.execute("SELECT operation FROM cursor").fetchone() == (
                0,
            )
            assert publisher.db.execute("SELECT count(*) FROM pages").fetchone() == (0,)
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, refused)


def test_private_journal_is_unreadable_to_watcher_and_cross_user_writes_denied(spool):
    root, handoff, _, _ = spool

    def watcher():
        with pytest.raises(PermissionError):
            (root / "publisher/journal.sqlite").read_bytes()
        with pytest.raises(PermissionError):
            (Path(handoff.outbox) / "write").write_text("forbidden")

    as_user(OBSERVER, watcher)

    def publisher():
        with pytest.raises(PermissionError):
            (Path(handoff.acknowledgments) / "write").write_text("forbidden")

    as_user(PUBLISHER, publisher)


@pytest.mark.parametrize(
    "fault",
    [
        "symlink",
        "fifo",
        "directory",
        "owner",
        "mode",
        "hardlink",
        "oversize",
        "filename",
        "policy",
        "noncanonical",
    ],
)
def test_public_file_permissions_and_content_fail_closed(spool, fault):
    _, handoff, _, _ = spool
    add_rows(spool, 1)
    publish(spool, initialize=True)
    path = public_paths(handoff.outbox)[0]
    raw = path.read_bytes()
    if fault == "symlink":
        path.unlink()
        path.symlink_to("/missing")
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path, 0o640)
        os.chown(path, PUBLISHER, GROUP)
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    elif fault == "owner":
        os.chown(path, OBSERVER, GROUP)
    elif fault == "mode":
        path.chmod(0o660)
    elif fault == "hardlink":
        os.link(path, path.with_name("b" * 64 + ".json"))
    elif fault == "oversize":
        path.write_bytes(b"x" * (MAX_BYTES + 1))
    elif fault == "filename":
        path.rename(path.with_name("b" * 64 + ".json"))
    elif fault == "policy":
        page = json.loads(raw)
        page["policy"] = "b" * 64
        path.write_text(canonical(page))
    else:
        path.write_bytes(raw + b"\n")

    def refused():
        config = spool[2]
        queue = ActivityQueue(
            spool[0] / "observer/queue.sqlite", config, initialize=True
        )
        try:
            with pytest.raises((ValueError, OSError, json.JSONDecodeError)):
                import_pages(queue, handoff)
            assert queue.pending_count() == 0
        finally:
            queue.close()

    as_user(OBSERVER, refused)


def test_observer_queue_backpressure_drains_without_import_cursor_loss(spool):
    root, handoff, config, _ = spool
    add_rows(spool, 3)
    publish(spool, initialize=True)

    def full():
        queue = ActivityQueue(root / "observer/queue.sqlite", config, initialize=True)
        try:
            for i in range(1000):
                queue.enqueue({"test_only": i})
            import_pages(queue, handoff)
            assert queue.db.execute(
                "SELECT count(*) FROM selector_pages"
            ).fetchone() == (0,)
            assert queue.pending_count() == 1000
            # Model drain: retain durable rows, free only accepted receipts.
            queue.db.execute("UPDATE pending SET receipt_id=?", ("a" * 64,))
            import_pages(queue, handoff)
            assert queue.pending_count() == 3
        finally:
            queue.close()

    as_user(OBSERVER, full)
    assert observe(spool)["delivered"] == 3
    assert publish(spool)["status"] == "waiting"


def test_immutable_write_and_directory_bounds(spool):
    _, handoff, _, _ = spool

    def writes():
        path = Path(handoff.outbox) / ("a" * 64 + ".json")
        write_public(path, {"one": 1}, owner=PUBLISHER, group=GROUP)
        write_public(path, {"one": 1}, owner=PUBLISHER, group=GROUP)
        with pytest.raises(ValueError, match="conflicts"):
            write_public(path, {"one": 2}, owner=PUBLISHER, group=GROUP)
        assert read_public(path, owner=PUBLISHER, group=GROUP) == {"one": 1}
        for i in range(64):
            (Path(handoff.outbox) / f".tmp-{i:032x}").touch()
        with pytest.raises(ValueError, match="bounded admission"):
            public_paths(handoff.outbox)

    as_user(PUBLISHER, writes)


@pytest.mark.parametrize("fault", ["owner", "mode", "symlink"])
def test_directory_scope_changes_refuse(spool, fault):
    root, handoff, _, _ = spool
    path = Path(handoff.outbox)
    if fault == "owner":
        os.chown(path, OBSERVER, GROUP)
    elif fault == "mode":
        path.chmod(0o770)
    else:
        target = root / "moved"
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ValueError, match="directory ownership"):
        handoff.directories()


def sample_page():
    row = {
        "stage": "service_distribution",
        "epoch_index": 9,
        "bucket_id": "gamma",
        "source_block": 120,
        "block": 130,
        "block_hash": h(130),
        "extrinsic_index": 0,
        "extrinsic_hash": h(130),
        "amount_atomic": 25,
        "reason": "Observe finalized collector distribution",
    }
    return {
        "version": 1,
        "policy": "a" * 64,
        "items": [{"operation": 1, "selection": row}],
    }


def test_allowlist_removes_private_fields_and_exact_content_identity():
    page = sample_page()
    config = SelectorHandoff("/outbox", "/acks", PUBLISHER, OBSERVER, GROUP, "a" * 64)
    row = page["items"][0]["selection"]
    assert selection({**row, "signed_json": "private"}) == row
    assert validate_page(page, config) == digest(page)
    page["items"].append(page["items"][0])
    with pytest.raises(ValueError, match="ordered"):
        validate_page(page, config)


@pytest.mark.parametrize(
    "key,value",
    [
        ("amount_atomic", True),
        ("amount_atomic", 2**53),
        ("source_block", 0),
        ("block_hash", "bad"),
        ("bucket_id", "../bad"),
    ],
)
def test_invalid_public_coordinates_refuse(key, value):
    row = sample_page()["items"][0]["selection"]
    with pytest.raises(ValueError):
        selection({**row, key: value})


def test_handoff_policy_and_legacy_queue_digest_are_immutable():
    config, _ = fixture()
    old_digest = hashlib.sha256(
        canonical(
            {
                "policy": config.approval.policy.digest,
                "settings": config.settings_checksum,
                "start_block": config.start_block,
            }
        ).encode()
    ).hexdigest()
    assert config.digest == old_digest
    handoff = SelectorHandoff(
        "/outbox",
        "/acks",
        PUBLISHER,
        OBSERVER,
        GROUP,
        config.approval.policy.collector_policy_digest,
    )
    assert replace(config, selector_handoff=handoff).digest != old_digest
    with pytest.raises(ValueError, match="policy differs"):
        replace(
            config, selector_handoff=replace(handoff, collector_policy_digest="a" * 64)
        )


def test_disabled_real_exporter_and_observer_do_not_open_spool_or_chain(tmp_path):
    root = Path(__file__).resolve().parents[2]
    config, _ = fixture()
    handoff = SelectorHandoff(
        "/nonexistent/outbox",
        "/nonexistent/acks",
        PUBLISHER,
        OBSERVER,
        GROUP,
        config.approval.policy.collector_policy_digest,
    )
    for script, body in (
        (
            "treasury_selector_publisher.py",
            {"enabled": False, "selector_handoff": asdict(handoff)},
        ),
        (
            "treasury_activity_observer.py",
            {
                "enabled": False,
                "approval": config.approval.model_dump(mode="json"),
                "settings_checksum": config.settings_checksum,
                "start_block": 120,
                "selector_handoff": asdict(handoff),
            },
        ),
    ):
        path = tmp_path / script
        raw = canonical(body).encode()
        path.write_bytes(raw)
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / script),
                "--config",
                str(path),
                "--config-sha256",
                hashlib.sha256(raw).hexdigest(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=root,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"status": "disabled", "authority": "none"}


def test_journal_and_public_handoff_cannot_be_combined_before_io():
    config, _ = fixture()
    handoff = SelectorHandoff(
        "/missing/outbox",
        "/missing/acks",
        PUBLISHER,
        OBSERVER,
        GROUP,
        config.approval.policy.collector_policy_digest,
    )
    with pytest.raises(ValueError, match="private transfer journal"):
        observer_tick(
            replace(config, selector_handoff=handoff),
            state_path=Path("/missing"),
            transfer_journal=Path("/private/journal"),
            chain=None,
            mcp=None,
        )


def test_actual_publisher_reconnect_retains_cursor_and_semantic_refusal_halts(spool):
    root, handoff, _, _ = spool
    add_rows(spool, 2)

    def exercise():
        publisher = SelectorPublisher(
            root / "publisher/state.sqlite", handoff, initialize=True
        )
        readers, sleeps, emitted = [], [], []

        class Stop(Exception):
            pass

        class Reader:
            def __init__(self, fails):
                self.fails, self.closed = fails, False

            def epoch_at(self, _block):
                if self.fails:
                    assert publisher.db.execute(
                        "SELECT operation FROM cursor"
                    ).fetchone() == (0,)
                    raise SelectorChainUnavailable("test network timeout")
                return 9

            def close(self):
                self.closed = True

        attempts = 0

        def factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SelectorChainUnavailable("test connect failure")
            reader = Reader(fails=attempts == 2)
            readers.append(reader)
            return reader

        def sleep(seconds):
            sleeps.append(seconds)
            if seconds == 60:
                raise Stop()

        try:
            with pytest.raises(Stop):
                run_publisher(
                    publisher,
                    root / "publisher/selector-snapshot.db",
                    factory,
                    sleep=sleep,
                    emit=emitted.append,
                )
            assert sleeps == [15, 30, 60]
            assert all(reader.closed for reader in readers)
            assert emitted[-1]["status"] == "published"
            assert publisher.db.execute("SELECT operation FROM cursor").fetchone() == (
                2,
            )
            # Runtime drift is terminal: no retry or cursor reset/new page.
            add = root / "publisher/journal.sqlite"
            with sqlite3.connect(add) as db:
                db.execute(
                    "INSERT INTO operations SELECT 3,role,state,source_block,bucket,"
                    "amount,"
                    "settlement_json,signed_json,call_json FROM operations WHERE id=1"
                )
            refresh_snapshot(spool)
            reader = Reader(False)

            def refused_epoch(_block):
                raise ValueError("runtime changed")

            reader.epoch_at = refused_epoch
            with pytest.raises(ValueError, match="runtime changed"):
                run_publisher(
                    publisher,
                    root / "publisher/selector-snapshot.db",
                    lambda: reader,
                    sleep=sleep,
                )
            assert publisher.db.execute("SELECT operation FROM cursor").fetchone() == (
                2,
            )
        finally:
            publisher.close()

    refresh_snapshot(spool)
    as_user(PUBLISHER, exercise)


def test_publisher_backoff_cap_and_state_error_is_terminal():
    class Publisher:
        def recover(self):
            pass

    class Stop(Exception):
        pass

    sleeps = []

    def unavailable():
        raise SelectorChainUnavailable("test transport unavailable")

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 7:
            raise Stop()

    with pytest.raises(Stop):
        run_publisher(Publisher(), None, unavailable, sleep=sleep)
    assert sleeps == [15, 30, 60, 120, 240, 300, 300]
    with pytest.raises(SelectorChainUnavailable):
        run_publisher(Publisher(), None, unavailable, once=True, sleep=sleep)

    class Broken(Publisher):
        def recover(self):
            raise sqlite3.OperationalError("private state unavailable")

    with pytest.raises(sqlite3.OperationalError):
        run_publisher(Broken(), None, unavailable, sleep=sleep)
    assert len(sleeps) == 7


def test_actual_sdk_transport_classifier_preserves_auth_runtime_and_rpc_errors():
    from async_substrate_interface.errors import (
        MaxRetriesExceeded,
        SubstrateRequestException,
    )
    from websockets.exceptions import ConnectionClosedError, InvalidHandshake
    from websockets.frames import Close

    from scripts.treasury_selector_publisher import chain_read

    def fail(error):
        def execute():
            raise error

        return execute

    for error in (
        ConnectionError(),
        TimeoutError(),
        MaxRetriesExceeded(),
        ConnectionClosedError(Close(1012, "test restart"), None),
    ):
        with pytest.raises(SelectorChainUnavailable):
            chain_read(fail(error))
    terminal = ConnectionClosedError(Close(1008, "test refusal"), None)
    exhausted = MaxRetriesExceeded()
    exhausted.__context__ = terminal
    for error in (
        ValueError("runtime differs"),
        SubstrateRequestException("RPC denied"),
        InvalidHandshake("auth"),
        terminal,
        exhausted,
        PermissionError(),
    ):
        with pytest.raises(type(error)):
            chain_read(fail(error))
