"""Same-host public selector spool; acknowledgments grant no funds authority.

The publisher reads its own private journal. A different observer user reads
only canonical selection pages and writes only delivery acknowledgments.
Platform independently verifies each selector through existing receipt ingress.
No caller finality, private journal payload or acknowledgment authorizes money.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from ditto.treasury.activity_export import export_snapshot_distributions
from ditto.treasury.collector import canonical

MAX_BYTES = 131072
SELECTION_FIELDS = (
    "stage",
    "epoch_index",
    "bucket_id",
    "source_block",
    "block",
    "block_hash",
    "extrinsic_index",
    "extrinsic_hash",
    "amount_atomic",
    "reason",
)


class SelectorChainUnavailable(RuntimeError):
    """Public chain transport unavailable; retained pages/cursor remain intact."""


def run_publisher(
    publisher,
    journal,
    reader_factory,
    *,
    once=False,
    poll_seconds=60,
    sleep=time.sleep,
    emit=lambda _value: None,
):
    """Reconnect only classified chain transport failures; state errors halt.

    A reader classifies errors exclusively around its public network calls.
    Semantic RPC, runtime/policy, filesystem and SQLite failures are not retries.
    No transaction submission is available in this loop.
    """
    reader = None
    delay = 15
    try:
        while True:
            try:
                # ACK retirement does not require a working public chain link.
                publisher.recover()
                if reader is None:
                    reader = reader_factory()
                emit(publisher.tick(journal, reader.epoch_at))
                delay = 15
                if once:
                    return
                sleep(poll_seconds)
            except SelectorChainUnavailable:
                if reader is not None:
                    reader.close()
                    reader = None
                if once:
                    raise
                emit(
                    {
                        "status": "chain_transport_backoff",
                        "retry_seconds": delay,
                        "authority": "none",
                    }
                )
                sleep(delay)
                delay = min(delay * 2, 300)
    finally:
        if reader is not None:
            reader.close()


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require_digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid selector digest")
    return value


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class SelectorHandoff:
    outbox: str
    acknowledgments: str
    publisher_uid: int
    observer_uid: int
    shared_gid: int
    collector_policy_digest: str
    max_pending_pages: int = 10

    def __post_init__(self):
        require_digest(self.collector_policy_digest)
        for value in (self.publisher_uid, self.observer_uid, self.shared_gid):
            if type(value) is not int or not 0 < value < 2**31:
                raise ValueError("explicit non-root selector identities required")
        if self.publisher_uid == self.observer_uid:
            raise ValueError("separate selector users required")
        if (
            type(self.max_pending_pages) is not int
            or not 1 <= self.max_pending_pages <= 10
        ):
            raise ValueError("bounded selector pending pages required")
        if self.outbox == self.acknowledgments or any(
            not Path(p).is_absolute() for p in (self.outbox, self.acknowledgments)
        ):
            raise ValueError("distinct absolute selector directories required")

    @property
    def digest(self):
        return digest(asdict(self))

    def directories(self):
        for path, owner in (
            (self.outbox, self.publisher_uid),
            (self.acknowledgments, self.observer_uid),
        ):
            info = Path(path).lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != owner
                or info.st_gid != self.shared_gid
                or stat.S_IMODE(info.st_mode) != 0o750
            ):
                raise ValueError("selector directory ownership or mode differs")


def read_public(path: Path, *, owner: int, group: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_gid != group
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o640
            or not 0 < info.st_size <= MAX_BYTES
        ):
            raise ValueError("selector page is not a bounded scoped regular file")
        raw = os.read(fd, MAX_BYTES + 1)
        if len(raw) != info.st_size or len(raw) > MAX_BYTES:
            raise ValueError("selector page is incomplete or exceeds bound")
        body = json.loads(raw)
        if canonical(body).encode() != raw:
            raise ValueError("selector page bytes are not canonical")
        return body
    finally:
        os.close(fd)


def write_public(path: Path, body, *, owner: int, group: int):
    raw = canonical(body).encode()
    if not 0 < len(raw) <= MAX_BYTES or os.geteuid() != owner:
        raise ValueError("invalid selector writer or page size")
    if path.exists() or path.is_symlink():
        if read_public(path, owner=owner, group=group) != body:
            raise ValueError("immutable selector page conflicts")
        return
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.fchown(fd, -1, group)
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(raw)
            output.flush()
            os.fsync(fd)
        # Only this owner can write the directory; publisher/observer execution
        # is separately locked by its private SQLite state.
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def public_paths(directory: str):
    paths = []
    with os.scandir(directory) as entries:
        for number, entry in enumerate(entries, start=1):
            if number > 64:
                raise ValueError("selector directory exceeds bounded admission")
            if re.fullmatch(r"\.tmp-[0-9a-f]{32}", entry.name):
                continue
            if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name) is None:
                raise ValueError("unexpected selector directory entry")
            paths.append(Path(directory) / entry.name)
    return sorted(paths)


def selection(row):
    """Allowlist public coordinates; never forward arbitrary journal fields."""
    result = {field: row[field] for field in SELECTION_FIELDS}
    if result["stage"] != "service_distribution" or result["reason"] != (
        "Observe finalized collector distribution"
    ):
        raise ValueError("unsupported distribution selector")
    if (
        not isinstance(result["bucket_id"], str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", result["bucket_id"]) is None
    ):
        raise ValueError("invalid selector bucket")
    for key in (
        "epoch_index",
        "source_block",
        "block",
        "extrinsic_index",
        "amount_atomic",
    ):
        value = result[key]
        minimum = 0 if key in {"epoch_index", "extrinsic_index"} else 1
        if type(value) is not int or not minimum <= value <= 2**53 - 1:
            raise ValueError("selector exceeds exact public MCP integer bounds")
    for key in ("block_hash", "extrinsic_hash"):
        if (
            not isinstance(result[key], str)
            or re.fullmatch(r"0x[0-9a-f]{64}", result[key]) is None
        ):
            raise ValueError("invalid selector chain coordinate")
    return result


def validate_page(body, config):
    if not isinstance(body, dict) or set(body) != {"version", "policy", "items"}:
        raise ValueError("invalid public selector page")
    if (
        type(body["version"]) is not int
        or body["version"] != 1
        or body["policy"] != (config.collector_policy_digest)
    ):
        raise ValueError("selector historical policy differs")
    items = body["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("bounded nonempty selector page required")
    previous = 0
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"operation", "selection"}:
            raise ValueError("invalid public selector item")
        operation = item["operation"]
        if type(operation) is not int or not previous < operation < 2**63:
            raise ValueError("strict ordered selector operations required")
        if (
            not isinstance(item["selection"], dict)
            or set(item["selection"]) != set(SELECTION_FIELDS)
            or selection(item["selection"]) != item["selection"]
        ):
            raise ValueError("noncanonical selector item")
        identity = digest(item["selection"])
        if identity in seen:
            raise ValueError("duplicate selector in page")
        seen.add(identity)
        previous = operation
    return digest(body)


def validate_ack(ack, page_id, page, config):
    expected = [(i["operation"], digest(i["selection"])) for i in page["items"]]
    if (
        not isinstance(ack, dict)
        or set(ack) != {"version", "page", "policy", "receipts"}
        or (
            type(ack["version"]) is not int
            or ack["version"] != 1
            or ack["page"] != page_id
            or ack["policy"] != config.collector_policy_digest
            or not isinstance(ack["receipts"], list)
            or len(ack["receipts"]) != len(expected)
        )
    ):
        raise ValueError("selector acknowledgment conflicts")
    for receipt, (operation, identity) in zip(ack["receipts"], expected, strict=True):
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"operation", "selection", "receipt"}
            or (
                type(receipt["operation"]) is not int
                or receipt["operation"] != operation
                or receipt["selection"] != identity
            )
        ):
            raise ValueError("selector acknowledgment item conflicts")
        require_digest(receipt["receipt"])


class SelectorPublisher:
    """Private durable cursor + canonical pages; source journal never writable."""

    def __init__(self, state: Path, config: SelectorHandoff, *, initialize=False):
        if os.geteuid() != config.publisher_uid:
            raise ValueError("publisher identity differs")
        config.directories()
        if initialize and any(
            any(Path(p).iterdir()) for p in (config.outbox, config.acknowledgments)
        ):
            raise ValueError(
                "existing selector spool requires recovery, not initialization"
            )
        directory = state.parent.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or (stat.S_IMODE(directory.st_mode) != 0o700)
        ):
            raise ValueError("private publisher state directory required")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if initialize:
            flags |= os.O_CREAT | os.O_EXCL
        self.fd = os.open(state, flags, 0o600)
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or (stat.S_IMODE(info.st_mode) != 0o600)
            ):
                raise ValueError("private publisher state file required")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(state, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if initialize:
                self.db.executescript(
                    "CREATE TABLE pin(digest TEXT);"
                    "CREATE TABLE cursor(operation INTEGER,pages INTEGER);"
                    "CREATE TABLE source_checkpoint(rows INTEGER,"
                    "last_operation INTEGER);"
                    "CREATE TABLE pages(id TEXT PRIMARY KEY,body TEXT NOT NULL,"
                    "acked INTEGER NOT NULL,retired INTEGER NOT NULL,ack TEXT);"
                    "CREATE INDEX pages_pending ON pages(acked,retired);"
                )
                self.db.execute("INSERT INTO pin VALUES(?)", (config.digest,))
                self.db.execute("INSERT INTO cursor VALUES(0,0)")
                self.db.execute("INSERT INTO source_checkpoint VALUES(0,0)")
            if self.db.execute("SELECT digest FROM pin").fetchall() != [
                (config.digest,)
            ]:
                raise ValueError("publisher policy/config changed; recovery required")
            if initialize:
                fsync_directory(state.parent)
            self.config = config
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            os.close(self.fd)
            raise

    def close(self):
        self.db.close()
        os.close(self.fd)

    def recover(self):
        config = self.config
        config.directories()
        cursors = self.db.execute("SELECT operation,pages FROM cursor").fetchall()
        latest = self.db.execute(
            "SELECT body FROM pages ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        position = json.loads(latest[0])["items"][-1]["operation"] if latest else 0
        total = self.db.execute("SELECT count(*) FROM pages").fetchone()[0]
        if cursors != [(position, total)]:
            raise ValueError("publisher cursor/page loss requires explicit recovery")
        count = self.db.execute(
            "SELECT count(*) FROM pages WHERE retired=0"
        ).fetchone()[0]
        if count > config.max_pending_pages:
            raise ValueError("publisher retained pages exceed bound")
        for path in public_paths(config.outbox):
            known = self.db.execute(
                "SELECT body,retired FROM pages WHERE id=?", (path.stem,)
            ).fetchone()
            if (
                known is None
                or known[1]
                or read_public(
                    path, owner=config.publisher_uid, group=config.shared_gid
                )
                != json.loads(known[0])
            ):
                raise ValueError(
                    "selector page loss/conflict requires explicit recovery"
                )
        for path in public_paths(config.acknowledgments):
            known = self.db.execute(
                "SELECT body,ack FROM pages WHERE id=?", (path.stem,)
            ).fetchone()
            if known is None:
                raise ValueError("unknown selector acknowledgment")
            page = json.loads(known[0])
            if validate_page(page, config) != path.stem:
                raise ValueError("publisher stored page conflicts")
            try:
                ack = read_public(
                    path, owner=config.observer_uid, group=config.shared_gid
                )
            except FileNotFoundError:
                # Observer may concurrently prune its own already consumed ACK.
                # No new page can advance from an absent acknowledgment.
                continue
            validate_ack(ack, path.stem, page, config)
            if known[1] is not None and known[1] != canonical(ack):
                raise ValueError("reused selector acknowledgment conflicts")
        for page_id, encoded, acked in self.db.execute(
            "SELECT id,body,acked FROM pages WHERE retired=0 ORDER BY rowid LIMIT 11"
        ).fetchall():
            page = json.loads(encoded)
            if validate_page(page, config) != page_id:
                raise ValueError("publisher stored page conflicts")
            path = Path(config.outbox) / (page_id + ".json")
            if acked:
                if path.exists() or path.is_symlink():
                    if (
                        read_public(
                            path, owner=config.publisher_uid, group=config.shared_gid
                        )
                        != page
                    ):
                        raise ValueError("acknowledged page conflicts")
                    path.unlink()
                self._retire(page_id)
                continue
            write_public(
                path, page, owner=config.publisher_uid, group=config.shared_gid
            )
            ack_path = Path(config.acknowledgments) / (page_id + ".json")
            if not ack_path.exists() and not ack_path.is_symlink():
                continue
            ack = read_public(
                ack_path, owner=config.observer_uid, group=config.shared_gid
            )
            validate_ack(ack, page_id, page, config)
            # Delivery acknowledgment only; never an earning/spend proof.
            self.db.execute(
                "UPDATE pages SET acked=1,ack=? WHERE id=?", (canonical(ack), page_id)
            )
            path.unlink()
            self._retire(page_id)

    def _retire(self, page_id):
        fsync_directory(self.config.outbox)
        self.db.execute("UPDATE pages SET retired=1 WHERE id=? AND acked=1", (page_id,))

    def tick(self, journal: Path, epoch_at):
        self.recover()
        if self.db.execute("SELECT count(*) FROM pages WHERE acked=0").fetchone()[
            0
        ] >= (self.config.max_pending_pages):
            return {"status": "backpressure", "authority": "none"}
        position = self.db.execute("SELECT operation FROM cursor").fetchone()[0]
        checkpoints = self.db.execute(
            "SELECT rows,last_operation FROM source_checkpoint"
        ).fetchall()
        if len(checkpoints) != 1:
            raise ValueError("snapshot checkpoint lost; explicit recovery required")
        rows, checkpoint = export_snapshot_distributions(
            journal,
            SimpleNamespace(digest=self.config.collector_policy_digest),
            epoch_at,
            after_id=position,
            minimum=checkpoints[0],
            validate_history=self._validate_snapshot_history,
        )
        if not rows:
            self.db.execute(
                "UPDATE source_checkpoint SET rows=?,last_operation=?", checkpoint
            )
            return {"status": "waiting", "authority": "none"}
        page = {
            "version": 1,
            "policy": self.config.collector_policy_digest,
            "items": [
                {"operation": r["journal_operation_id"], "selection": selection(r)}
                for r in rows
            ],
        }
        page_id = validate_page(page, self.config)
        if len(canonical(page).encode()) > MAX_BYTES:
            raise ValueError("selector page exceeds bound")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO pages VALUES(?,?,0,0,NULL)", (page_id, canonical(page))
            )
            self.db.execute(
                "UPDATE cursor SET operation=?,pages=pages+1",
                (rows[-1]["journal_operation_id"],),
            )
            self.db.execute(
                "UPDATE source_checkpoint SET rows=?,last_operation=?", checkpoint
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.recover()
        return {
            "status": "published",
            "page": page_id,
            "count": len(rows),
            "authority": "none",
        }

    def _validate_snapshot_history(self, source):
        # Retained publication evidence is never discarded. Validate every past
        # selector against the single immutable snapshot read transaction,
        # streaming one bounded page at a time instead of loading all history.
        for page_id, encoded in self.db.execute(
            "SELECT id,body FROM pages ORDER BY rowid"
        ):
            page = json.loads(encoded)
            if validate_page(page, self.config) != page_id:
                raise ValueError("retained publisher page content identity differs")
            for item in page["items"]:
                row = source.execute(
                    "SELECT state,source_block,bucket,amount,settlement_json "
                    "FROM operations "
                    "WHERE role='transfer' AND id=?",
                    (item["operation"],),
                ).fetchone()
                expected = item["selection"]
                if row is None or tuple(row[:4]) != (
                    "finalized",
                    expected["source_block"],
                    expected["bucket_id"],
                    expected["amount_atomic"],
                ):
                    raise ValueError("snapshot published history lost or changed")
                settlement = json.loads(row[4])
                if settlement.get("status") != "finalized" or any(
                    settlement.get(key) != expected[key]
                    for key in (
                        "block",
                        "block_hash",
                        "extrinsic_index",
                        "extrinsic_hash",
                    )
                ):
                    raise ValueError("snapshot published coordinates changed")


def import_pages(queue, config: SelectorHandoff):
    """Durable admission only; existing observer performs MCP proof/ACK matching."""
    if os.geteuid() != config.observer_uid:
        raise ValueError("observer selector identity differs")
    config.directories()
    paths = public_paths(config.outbox)
    if len(paths) > config.max_pending_pages:
        raise ValueError("selector pages exceed approved backpressure bound")
    for path in paths:
        try:
            page = read_public(
                path, owner=config.publisher_uid, group=config.shared_gid
            )
        except FileNotFoundError:
            # Publisher may retire an ACKed page between listing and opening.
            # Unacknowledged pages remain retained in its private state.
            continue
        page_id = validate_page(page, config)
        if page_id != path.stem:
            raise ValueError("selector page filename/content conflicts")
        known = queue.db.execute(
            "SELECT body FROM selector_pages WHERE id=?", (page_id,)
        ).fetchone()
        if known is not None:
            if known[0] != canonical(page):
                raise ValueError("observer persisted page conflicts")
            continue
        selections = [i["selection"] for i in page["items"]]
        queue.db.execute("BEGIN IMMEDIATE")
        try:
            if not queue.can_enqueue(selections):
                queue.db.execute("ROLLBACK")
                break
            for item in selections:
                queue.enqueue(item)
            queue.db.execute(
                "INSERT INTO selector_pages VALUES(?,?)", (page_id, canonical(page))
            )
            queue.db.execute("COMMIT")
        except BaseException:
            queue.db.execute("ROLLBACK")
            raise


def _ack_body(queue, config, page_id, encoded):
    page = json.loads(encoded)
    if validate_page(page, config) != page_id:
        raise ValueError("observer persisted selector page conflicts")
    receipts = []
    for item in page["items"]:
        identity = digest(item["selection"])
        result = queue.db.execute(
            "SELECT body,receipt_id FROM pending WHERE id=?",
            (identity,),
        ).fetchone()
        if result is None or result[0] != canonical(item["selection"]):
            raise ValueError("durable observer selector missing or conflicting")
        if result[1] is None:
            return None
        require_digest(result[1])
        receipts.append(
            {
                "operation": item["operation"],
                "selection": identity,
                "receipt": result[1],
            }
        )
    return {
        "version": 1,
        "page": page_id,
        "policy": config.collector_policy_digest,
        "receipts": receipts,
    }


def acknowledge_pages(queue, config: SelectorHandoff):
    """Publish only after every selector has a durable matched receipt ID.

    No HTTP status alone can acknowledge a page. An unknown response/crash before
    durable per-selector acceptance leaves the page pending for idempotent replay.
    """
    if os.geteuid() != config.observer_uid:
        raise ValueError("observer selector identity differs")
    config.directories()
    active = {p.stem for p in public_paths(config.outbox)}
    if len(active) > config.max_pending_pages:
        raise ValueError("selector pages exceed approved backpressure bound")
    removed = False
    for path in public_paths(config.acknowledgments):
        if path.stem not in active:
            # Publisher commits its ACK before removing its page; only this
            # observer can clean its own acknowledgment, never publisher state.
            stored = queue.db.execute(
                "SELECT body FROM selector_pages WHERE id=?", (path.stem,)
            ).fetchone()
            if stored is None:
                raise ValueError("unknown observer acknowledgment cannot be pruned")
            expected = _ack_body(queue, config, path.stem, stored[0])
            if (
                expected is None
                or read_public(path, owner=config.observer_uid, group=config.shared_gid)
                != expected
            ):
                raise ValueError("conflicting observer acknowledgment cannot be pruned")
            path.unlink()
            removed = True
    if removed:
        fsync_directory(config.acknowledgments)
    for page_id in sorted(active):
        stored = queue.db.execute(
            "SELECT body FROM selector_pages WHERE id=?", (page_id,)
        ).fetchone()
        if stored is None:
            continue
        body = _ack_body(queue, config, page_id, stored[0])
        if body is None:
            continue
        write_public(
            Path(config.acknowledgments) / (page_id + ".json"),
            body,
            owner=config.observer_uid,
            group=config.shared_gid,
        )
