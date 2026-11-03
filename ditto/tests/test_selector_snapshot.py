"""Source transaction, allowlisted snapshot, atomic failure and real CLI hook."""

import json
import os
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto.tests.test_selector_handoff import (
    PUBLISHER,
    add_rows,
    as_user,
    observe,
    publish,
    refresh_snapshot,
)
from ditto.tests.test_selector_handoff import (
    spool as spool,
)
from ditto.treasury.activity_export import (
    write_selector_snapshot,
)
from ditto.treasury.selector_handoff import SelectorPublisher


def test_streamed_snapshot_keeps_full_history_without_private_fields(spool):
    root, handoff, _, _ = spool
    add_rows(spool, 1101)
    refresh_snapshot(spool)
    path = root / "publisher/selector-snapshot.db"
    assert b"PRIVATE_" not in path.read_bytes()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert db.execute("SELECT * FROM snapshot_meta").fetchone() == (1, 1101, 1101)
        assert [row[1] for row in db.execute("PRAGMA table_info(operations)")] == (
            [
                "id",
                "role",
                "state",
                "source_block",
                "bucket",
                "amount",
                "settlement_json",
            ]
        )
        assert db.execute("SELECT digest,role FROM pin").fetchone() == (
            handoff.collector_policy_digest,
            "transfer",
        )
    assert not any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    )
    assert publish(spool, initialize=True)["count"] == 100


@pytest.mark.parametrize(
    "fault", ["missing", "truncated", "rollback", "same_height_mutation"]
)
def test_snapshot_loss_rollback_or_published_history_change_cannot_reset_cursor(
    spool, fault
):
    root, handoff, _, _ = spool
    add_rows(spool, 3)
    publish(spool, initialize=True)
    observe(spool, initialize=True)
    publish(spool)
    path = root / "publisher/selector-snapshot.db"
    if fault == "missing":
        path.unlink()
    elif fault == "truncated":
        path.write_bytes(b"corrupt")
    else:
        with sqlite3.connect(path) as db:
            if fault == "rollback":
                db.execute("DELETE FROM operations WHERE id=3")
                db.execute("UPDATE snapshot_meta SET rows=2,last_operation=2")
            else:
                db.execute("UPDATE operations SET amount=26 WHERE id=1")

    def refused():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", handoff)
        try:
            with pytest.raises((ValueError, OSError, sqlite3.DatabaseError)):
                publisher.tick(path, lambda _: 9)
            assert publisher.db.execute("SELECT operation FROM cursor").fetchone() == (
                3,
            )
        finally:
            publisher.close()

    as_user(PUBLISHER, refused)


def test_snapshot_crash_before_replace_keeps_prior_valid_image(spool, monkeypatch):
    root, _, _, _ = spool
    add_rows(spool, 1)
    refresh_snapshot(spool)
    path = root / "publisher/selector-snapshot.db"
    before = path.read_bytes()
    add_rows(spool, 1, start=2)

    def crash(*_args):
        raise RuntimeError("injected pre-replace crash")

    monkeypatch.setattr(os, "replace", crash)

    def refused():
        with pytest.raises(RuntimeError, match="pre-replace"):
            refresh_snapshot(spool)

    as_user(PUBLISHER, refused)
    assert path.read_bytes() == before


@pytest.mark.parametrize("fault", ["page_loss", "page_mutation"])
def test_retained_publisher_history_cannot_disappear_or_change(spool, fault):
    root, handoff, _, _ = spool
    add_rows(spool, 101)
    publish(spool, initialize=True)
    observe(spool, initialize=True)
    publish(spool)
    with sqlite3.connect(root / "publisher/state.sqlite") as db:
        page_id, encoded = db.execute(
            "SELECT id,body FROM pages ORDER BY rowid LIMIT 1"
        ).fetchone()
        if fault == "page_loss":
            db.execute("DELETE FROM pages WHERE id=?", (page_id,))
        else:
            from ditto.treasury.collector import canonical

            body = json.loads(encoded)
            body["items"][0]["selection"]["amount_atomic"] += 1
            db.execute("UPDATE pages SET body=? WHERE id=?", (canonical(body), page_id))

    def refused():
        publisher = SelectorPublisher(root / "publisher/state.sqlite", handoff)
        try:
            with pytest.raises(ValueError):
                publisher.tick(root / "publisher/selector-snapshot.db", lambda _: 9)
        finally:
            publisher.close()

    as_user(PUBLISHER, refused)


@pytest.mark.parametrize(
    "fault",
    ["source_alias", "symlink", "hardlink", "mode", "owner", "pin", "transaction"],
)
def test_snapshot_cannot_overwrite_source_or_wrong_scope(spool, fault):
    root, handoff, _, _ = spool
    add_rows(spool, 1)
    refresh_snapshot(spool)
    path = root / "publisher/selector-snapshot.db"
    source = root / "publisher/journal.sqlite"
    if fault == "source_alias":
        path = source
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(source)
    elif fault == "hardlink":
        path.unlink()
        os.link(source, path)
    elif fault == "mode":
        path.chmod(0o640)
    elif fault == "owner":
        os.chown(path, 10002, 10003)
    elif fault == "pin":
        with sqlite3.connect(source) as db:
            db.execute("UPDATE pin SET digest=?", ("a" * 64,))

    def refused():
        with sqlite3.connect(source) as db:
            if fault == "transaction":
                db.execute("BEGIN")
            with pytest.raises(ValueError):
                write_selector_snapshot(
                    db, path, SimpleNamespace(digest=handoff.collector_policy_digest)
                )

    as_user(PUBLISHER, refused)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_real_sqlite_sidecar_alias_refuses_without_source_damage(spool, suffix):
    root, handoff, _, _ = spool
    add_rows(spool, 1)
    refresh_snapshot(spool)
    source = root / "publisher/journal.sqlite"
    prior = (root / "publisher/selector-snapshot.db").read_bytes()

    def exercise():
        with sqlite3.connect(source) as writer:
            writer.execute(
                "PRAGMA journal_mode=" + ("DELETE" if suffix == "-journal" else "WAL")
            )
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE operations SET amount=31 WHERE id=1")
            if suffix != "-journal":
                writer.commit()
            # Rollback journal remains live while a separate connection's
            # snapshot attempt must refuse before beginning any source read.
            reader = sqlite3.connect(source) if suffix == "-journal" else writer
            try:
                sidecar = Path(str(source) + suffix)
                assert sidecar.is_file()
                before, main_before = sidecar.read_bytes(), source.read_bytes()
                with pytest.raises(ValueError, match="journal or sidecar"):
                    write_selector_snapshot(
                        reader,
                        sidecar,
                        SimpleNamespace(digest=handoff.collector_policy_digest),
                    )
                assert sidecar.read_bytes() == before
                assert source.read_bytes() == main_before
                assert writer.execute(
                    "SELECT amount FROM operations WHERE id=1"
                ).fetchone() == (31,)
                assert (root / "publisher/selector-snapshot.db").read_bytes() == prior
            finally:
                if reader is not writer:
                    reader.close()

    as_user(PUBLISHER, exercise)


def test_actual_collector_cli_optional_hook_success_and_failure_do_not_retry_money(
    spool,
):
    root, handoff, _, _ = spool
    add_rows(spool, 2)

    def exercise():
        import scripts.treasury_collector as cli

        policy = SimpleNamespace(digest=handoff.collector_policy_digest, enabled=True)
        calls = []

        class Journal:
            def __init__(self, path, *_args):
                self.db = sqlite3.connect(path)

            def close(self):
                self.db.close()

        original = (
            cli.load_policy,
            cli.CollectorJournal,
            cli.PublicCollectorChain,
            cli.tick,
            cli.write_selector_snapshot,
            sys.argv,
            sys.modules.get("bittensor"),
        )
        try:
            cli.load_policy = lambda *_args: policy
            cli.CollectorJournal = Journal
            cli.PublicCollectorChain = lambda *_args, **_kwargs: None

            def tick(*_args):
                calls.append("durable_money_tick")
                return "distributed"

            cli.tick = tick
            sys.modules["bittensor"] = SimpleNamespace(
                Subtensor=lambda **_kwargs: nullcontext(SimpleNamespace(substrate=None))
            )
            sys.argv = [
                "collector",
                "--role",
                "transfer",
                "--policy",
                "/synthetic",
                "--policy-sha256",
                handoff.collector_policy_digest,
                "--journal",
                str(root / "publisher/journal.sqlite"),
                "--selector-snapshot",
                str(root / "publisher/selector-snapshot.db"),
            ]
            cli.main()
            assert (root / "publisher/selector-snapshot.db").exists()
            sys.argv.append("--snapshot-only")
            cli.main()
            assert len(calls) == 1  # observation repair never calls money tick
            sys.argv.pop()

            def failure(*_args):
                raise ValueError("snapshot observation failed")

            cli.write_selector_snapshot = failure
            with pytest.raises(SystemExit) as refusal:
                cli.main()
            assert refusal.value.code == 1
            assert len(calls) == 2  # once per invocation, never retried for snapshot
        finally:
            (
                cli.load_policy,
                cli.CollectorJournal,
                cli.PublicCollectorChain,
                cli.tick,
                cli.write_selector_snapshot,
                sys.argv,
                bt_module,
            ) = original
            if bt_module is None:
                sys.modules.pop("bittensor", None)
            else:
                sys.modules["bittensor"] = bt_module

    # Bound child returns structured results; CLI output cannot pollute the pipe.
    as_user(PUBLISHER, exercise)
