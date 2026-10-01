"""Default-off read-only watcher with a durable public-MCP delivery queue.

It never signs, broadcasts, installs credentials or trusts journal finality.
Platform independently validates every selection before its atomic projection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from ditto.treasury.activity_export import export_finalized_distributions
from ditto.treasury.collector import canonical
from ditto.treasury.selector_handoff import (
    SelectorHandoff,
    acknowledge_pages,
    fsync_directory,
    import_pages,
)
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    verify_policy_approval,
    verify_public_signature,
)

PUBLIC_MCP = "https://backroom.dittobench.ai/mcp"
MAX_PENDING = 1000


def observer_token(path: Path | None, *, environment_token: str = "") -> str:
    """Read an already approved private credential; never mint or log it.

    A supplied file is authoritative. Failure cannot fall back to an environment
    grant. systemd LoadCredential mounts a private per-unit file for this path.
    """
    if path is None:
        token = environment_token
    else:
        if environment_token:
            raise ValueError("ambiguous observer credential bindings")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o077
                or not 0 < info.st_size <= 8192
            ):
                raise ValueError("observer credential must be a bounded private file")
            raw = os.read(fd, 8193)
            if len(raw) > 8192:
                raise ValueError("observer credential exceeds bound")
            token = raw.decode("ascii").removesuffix("\n")
        finally:
            os.close(fd)
    if (
        not token
        or len(token) > 8192
        or not token.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token)
    ):
        raise ValueError("approved observer OAuth binding absent or malformed")
    return token


class ObservationUnavailable(RuntimeError):
    """Transient transport outage; durable selections may be safely redelivered."""


def run_observer(
    config,
    *,
    state_path,
    transfer_journal,
    chain,
    mcp_factory,
    poll_seconds=60,
    once=False,
    sleep=time.sleep,
    emit=lambda _value: None,
    initialize_selector_state=False,
):
    """Reconnect with bounded backoff; semantic/identity refusals still halt.

    OAuth expiration requires separate operator reauthorization. This loop never
    refreshes credentials or retries money movement, only durable observations.
    """
    if not config.enabled:
        emit({"status": "disabled", "authority": "none"})
        return
    if initialize_selector_state and not once:
        raise ValueError("selector initialization requires one explicit tick")
    mcp = None
    delay = 15
    try:
        while True:
            try:
                if mcp is None:
                    mcp = mcp_factory()
                result = observer_tick(
                    config,
                    state_path=state_path,
                    transfer_journal=transfer_journal,
                    chain=chain,
                    mcp=mcp,
                    initialize_selector_state=initialize_selector_state,
                )
                emit(result)
                delay = 15
                if once:
                    return
                sleep(poll_seconds)
            except ObservationUnavailable:
                if mcp is not None:
                    mcp.close()
                    mcp = None
                if once:
                    raise
                emit(
                    {
                        "status": "transport_backoff",
                        "retry_seconds": delay,
                        "authority": "none",
                    }
                )
                sleep(delay)
                delay = min(delay * 2, 300)
    finally:
        if mcp is not None:
            mcp.close()


class PublicActivityMCP:
    """Standard streamable HTTP MCP over the one public OAuth endpoint.

    The caller binds an existing separately approved token. No token mint,
    refresh, copied desktop credentials, private URL or redirect is supported.
    """

    def __init__(self, token: str, *, transport=None):
        if not token or "\n" in token or "\r" in token:
            raise ValueError("approved observer OAuth binding absent")
        self.client = httpx.Client(
            transport=transport, timeout=60, follow_redirects=False
        )
        self.headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        self.next_id = 0
        try:
            self.request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "sn118-treasury-activity-observer",
                        "version": "1",
                    },
                },
            )
            self.request("notifications/initialized", {}, notification=True)
            tools = self.request("tools/list", {})
            expected = {"get_treasury_settings", "record_treasury_receipt"}
            if (
                not isinstance(tools, dict)
                or tools.get("nextCursor")
                or not isinstance(tools.get("tools"), list)
                or len(tools["tools"]) != 2
                or any(not isinstance(item, dict) for item in tools["tools"])
                or {item.get("name") for item in tools["tools"]} != expected
            ):
                raise ValueError("a dedicated receipt-only OAuth grant is required")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.client.close()

    def request(self, method: str, params: dict, *, notification=False):
        try:
            return self._request(method, params, notification=notification)
        except (httpx.TransportError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ObservationUnavailable(
                "public MCP transport unavailable; delivery retained"
            ) from exc

    def _request(self, method: str, params: dict, *, notification=False):
        self.next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.next_id
        with self.client.stream(
            "POST", PUBLIC_MCP, headers=self.headers, json=body
        ) as response:
            if response.status_code not in {200, 202, 204}:
                if response.status_code == 429 or response.status_code >= 500:
                    raise ObservationUnavailable(
                        "public MCP temporarily unavailable; delivery retained"
                    )
                raise ValueError(
                    "public MCP observation unavailable; delivery retained"
                )
            session = response.headers.get("mcp-session-id")
            if session:
                if len(session) > 200 or any(c in session for c in "\r\n"):
                    raise ValueError("invalid public MCP session")
                self.headers["Mcp-Session-Id"] = session
            raw = bytearray()
            for part in response.iter_bytes():
                raw.extend(part)
                if len(raw) > 1_048_576:
                    raise ValueError("public MCP response exceeds observation bound")
            if notification:
                return None
            if "text/event-stream" in response.headers.get("content-type", ""):
                messages = [
                    json.loads(line[5:].strip())
                    for line in raw.decode().splitlines()
                    if line.startswith("data:")
                ]
                matches = [
                    m
                    for m in messages
                    if isinstance(m, dict)
                    and type(m.get("id")) is int
                    and m.get("id") == self.next_id
                ]
                if len(matches) != 1:
                    raise ObservationUnavailable(
                        "public MCP result absent; delivery retained"
                    )
                payload = matches[0]
            else:
                payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or payload.get("id") != self.next_id
        ):
            raise ObservationUnavailable(
                "public MCP acknowledgment absent; delivery retained"
            )
        if "error" in payload:
            raise ValueError("public MCP explicitly refused observation")
        if "result" not in payload:
            raise ObservationUnavailable("public MCP result absent; delivery retained")
        return payload["result"]

    def call(self, name: str, arguments: dict):
        if name not in {"get_treasury_settings", "record_treasury_receipt"}:
            raise ValueError("observer cannot call a general administrative tool")
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError("receipt refused; cursor and pending proof retained")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        content = result.get("content")
        if (
            not isinstance(content, list)
            or len(content) != 1
            or content[0].get("type") != "text"
        ):
            raise ObservationUnavailable("public MCP structured receipt absent")
        return json.loads(content[0]["text"])


@dataclass(frozen=True)
class ActivityObserverConfig:
    approval: TreasuryPolicyApproval
    settings_checksum: str
    start_block: int
    enabled: bool = False
    max_blocks: int = 16
    max_deliveries: int = 100
    selector_handoff: SelectorHandoff | None = None

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or type(self.start_block) is not int
            or self.start_block < 1
            or type(self.max_blocks) is not int
            or type(self.max_deliveries) is not int
            or not 1 <= self.max_blocks <= 32
            or not 1 <= self.max_deliveries <= 100
        ):
            raise ValueError("invalid bounded observer configuration")
        if len(self.settings_checksum) != 64:
            raise ValueError("immutable historical settings checksum absent")
        bytes.fromhex(self.settings_checksum)
        if self.selector_handoff is not None and (
            self.selector_handoff.collector_policy_digest
            != self.approval.policy.collector_policy_digest
        ):
            raise ValueError("selector handoff collector policy differs")

    @property
    def digest(self):
        body = {
            "policy": self.approval.policy.digest,
            "settings": self.settings_checksum,
            "start_block": self.start_block,
        }
        if self.selector_handoff is not None:
            body["selector_handoff"] = self.selector_handoff.digest
        return hashlib.sha256(canonical(body).encode()).hexdigest()


class ActivityQueue:
    """Private pending selections survive unknown delivery without new money effects."""

    def __init__(self, path: Path, config: ActivityObserverConfig, *, initialize=False):
        handoff = config.selector_handoff
        if initialize and handoff is None:
            raise ValueError("selector initialization requires approved handoff")
        if handoff is not None:
            handoff.directories()
            if os.geteuid() != handoff.observer_uid:
                raise ValueError("observer selector identity differs")
            if initialize and any(Path(handoff.acknowledgments).iterdir()):
                raise ValueError("existing acknowledgments require recovery, not reset")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = path.parent.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_mode & 0o077
            or directory.st_uid != os.geteuid()
        ):
            raise ValueError("observer state directory must be private and owned")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if handoff is None:
            flags |= os.O_CREAT
        elif initialize:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
            ):
                raise ValueError("observer queue must be private and owned")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        try:
            self.db = sqlite3.connect(path, isolation_level=None)
            self._initialize(
                config, existing_handoff=handoff is not None and not initialize
            )
            if initialize:
                fsync_directory(path.parent)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            os.close(self.fd)
            raise

    def _initialize(self, config: ActivityObserverConfig, *, existing_handoff=False):
        if existing_handoff:
            # Never recreate tables after interrupted initialization or state
            # loss: missing pending/page history cannot be safely inferred.
            required = {
                "pin": "digest",
                "cursor": "id,block,hash",
                "journal_cursor": "id,operation",
                "block_progress": "id,block,hash,event_offset",
                "pending": "id,body,receipt_id",
                "selector_pages": "id,body",
            }
            tables = {
                row[0]
                for row in self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            error = "selector observer state lost or changed; recovery required"
            if not required.keys() <= tables:
                raise ValueError(error)
            try:
                for table, columns in required.items():
                    self.db.execute(f"SELECT {columns} FROM {table} LIMIT 1")
                pins = self.db.execute("SELECT digest FROM pin").fetchall()
            except sqlite3.DatabaseError as exc:
                raise ValueError(error) from exc
            if pins != [(config.digest,)]:
                raise ValueError(error)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS pin(id INTEGER PRIMARY KEY "
            "CHECK(id=1),digest TEXT NOT NULL); CREATE TABLE IF NOT "
            "EXISTS cursor(id INTEGER PRIMARY KEY,block INTEGER NOT "
            "NULL,hash TEXT NOT NULL); CREATE TABLE IF NOT EXISTS "
            "journal_cursor(id INTEGER PRIMARY KEY "
            "CHECK(id=1),operation INTEGER NOT NULL); CREATE TABLE "
            "IF NOT EXISTS block_progress(id INTEGER PRIMARY KEY "
            "CHECK(id=1),block INTEGER NOT NULL,hash TEXT NOT NULL,"
            "event_offset INTEGER NOT NULL); CREATE TABLE "
            "IF NOT EXISTS pending(id TEXT PRIMARY KEY,body TEXT "
            "NOT NULL,receipt_id TEXT);"
        )
        pins = self.db.execute("SELECT digest FROM pin").fetchall()
        if pins and pins != [(config.digest,)]:
            raise ValueError(
                "observer historical policy changed; explicit recovery required"
            )
        self.db.execute("INSERT OR IGNORE INTO pin VALUES(1,?)", (config.digest,))
        if config.selector_handoff is not None and not existing_handoff:
            self.db.execute(
                "CREATE TABLE selector_pages(id TEXT PRIMARY KEY,body TEXT NOT NULL)"
            )

    def close(self):
        self.db.close()
        os.close(self.fd)

    def enqueue(self, selection: dict):
        encoded = canonical(selection)
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO pending VALUES(?,?,NULL)", (identity, encoded)
        )

    def pending_count(self) -> int:
        return self.db.execute(
            "SELECT count(*) FROM pending WHERE receipt_id IS NULL"
        ).fetchone()[0]

    def can_enqueue(self, selections: list[dict]) -> bool:
        # Count only previously unseen selectors. Replays do not consume room.
        identities = {
            hashlib.sha256(canonical(s).encode()).hexdigest() for s in selections
        }
        new = sum(
            self.db.execute("SELECT 1 FROM pending WHERE id=?", (identity,)).fetchone()
            is None
            for identity in identities
        )
        return self.pending_count() + new <= MAX_PENDING


def observer_tick(
    config: ActivityObserverConfig,
    *,
    state_path: Path,
    transfer_journal: Path | None,
    chain: Any,
    mcp: Any,
    initialize_selector_state: bool = False,
) -> dict:
    if not config.enabled:
        return {"status": "disabled", "authority": "none"}
    if config.selector_handoff is not None and transfer_journal is not None:
        raise ValueError("separate observer cannot also read private transfer journal")
    policy = verify_policy_approval(
        config.approval,
        expected_policy_digest=config.approval.policy.digest,
        expected_collector_policy_digest=config.approval.policy.collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    control = mcp.call("get_treasury_settings", {"revision": policy.revision})
    revisions = [r for r in control["history"] if r["revision"] == policy.revision]
    if (
        len(revisions) != 1
        or revisions[0]["checksum"] != config.settings_checksum
        or hashlib.sha256(canonical(revisions[0]["settings"]).encode()).hexdigest()
        != config.settings_checksum
    ):
        raise ValueError(
            "configured historical observer settings unavailable or changed"
        )
    settings = revisions[0]["settings"]
    buckets = settings.get("service_buckets", [])
    if (
        settings.get("allocation_version") != 2
        or (settings.get("treasury_hotkey"), settings.get("treasury_coldkey"))
        != (policy.collector_hotkey, policy.collector_coldkey)
        or sorted(
            (b["bucket_id"], b["holding_coldkey"], b["allocation_bps"]) for b in buckets
        )
        != sorted(
            (b.bucket_id, b.holding_coldkey, b.allocation_bps) for b in policy.buckets
        )
    ):
        raise ValueError("observer destinations differ from historical offline policy")
    queue = ActivityQueue(state_path, config, initialize=initialize_selector_state)
    try:
        if config.selector_handoff is not None:
            import_pages(queue, config.selector_handoff)
        cursor = queue.db.execute("SELECT block,hash FROM cursor WHERE id=1").fetchone()
        if cursor and chain.block_hash(cursor[0]) != cursor[1]:
            raise ValueError(
                "finalized observer cursor reorg; explicit recovery required"
            )
        progress = queue.db.execute(
            "SELECT block,hash,event_offset FROM block_progress WHERE id=1"
        ).fetchone()
        if progress and (
            chain.block_hash(progress[0]) != progress[1]
            or progress[0] != (cursor[0] + 1 if cursor else config.start_block)
        ):
            raise ValueError(
                "partial finalized block changed; explicit recovery required"
            )
        # Old/near-full queues still drain. Admission is bounded before cursor
        # advancement, rather than refusing the very work that frees capacity.
        if transfer_journal is not None:
            # Read-only, allowlisted journal export; no delegate/GCP/signed fields.
            position = queue.db.execute(
                "SELECT operation FROM journal_cursor WHERE id=1"
            ).fetchone()
            for selection in export_finalized_distributions(
                transfer_journal,
                SimpleNamespace(digest=policy.collector_policy_digest),
                chain.epoch_at,
                after_id=position[0] if position else 0,
            ):
                operation = selection.pop("journal_operation_id")
                queue.db.execute("BEGIN IMMEDIATE")
                try:
                    if not queue.can_enqueue([selection]):
                        queue.db.execute("ROLLBACK")
                        break
                    queue.enqueue(selection)
                    queue.db.execute(
                        (
                            "INSERT INTO journal_cursor VALUES(1,?) ON CONFLICT(id) "
                            "DO UPDATE SET operation=excluded.operation"
                        ),
                        (operation,),
                    )
                    queue.db.execute("COMMIT")
                except BaseException:
                    queue.db.execute("ROLLBACK")
                    raise
        last = cursor[0] if cursor else config.start_block - 1
        end = min(chain.finalized_height(), last + config.max_blocks)
        for block in range(last + 1, end + 1):
            at, epoch, events, encoded = chain.finalized_payment_block(block)
            offset = progress[2] if progress and progress[0] == block else 0
            if not 0 <= offset <= len(events):
                raise ValueError("partial block event checkpoint invalid")
            next_offset = offset
            selections = []
            for position, event in enumerate(events[offset:], start=offset):
                next_offset = position + 1
                if (
                    event.get("module_id"),
                    event.get("event_id"),
                    event.get("phase"),
                ) != ("Balances", "Transfer", "ApplyExtrinsic"):
                    continue
                attrs = event.get("event", {}).get("attributes")
                if not isinstance(attrs, dict) or set(attrs) != {
                    "from",
                    "to",
                    "amount",
                }:
                    raise ValueError("unsupported observer payment schema")
                event_matches = 0
                for bucket in buckets:
                    for rule in bucket.get("payee_rules", []):
                        if (
                            rule.get("enabled", True)
                            and rule["asset"] == "TAO"
                            and (attrs["from"], attrs["to"])
                            == (bucket["holding_coldkey"], rule["recipient_coldkey"])
                        ):
                            event_matches += 1
                            if event_matches > 1:
                                raise ValueError(
                                    "ambiguous historical vendor payment rules"
                                )
                            index = event.get("extrinsic_idx")
                            amount = attrs["amount"]
                            if (
                                type(index) is not int
                                or not 0 <= index < len(encoded)
                                or type(amount) is not int
                                or not 0 < amount <= 2**53 - 1
                            ):
                                raise ValueError(
                                    "observer selector exceeds exact public MCP bounds"
                                )
                            extrinsic_hash = (
                                "0x"
                                + hashlib.blake2b(
                                    bytes.fromhex(encoded[index][2:]), digest_size=32
                                ).hexdigest()
                            )
                            selections.append(
                                {
                                    "stage": "vendor_payment",
                                    "epoch_index": epoch,
                                    "bucket_id": bucket["bucket_id"],
                                    "source_block": None,
                                    "block": block,
                                    "block_hash": at,
                                    "extrinsic_index": index,
                                    "extrinsic_hash": extrinsic_hash,
                                    "amount_atomic": amount,
                                    "payee_rule_id": rule["rule_id"],
                                    "parent_receipt_id": None,
                                    "reason": "Observe configured vendor payment",
                                }
                            )
                if len(selections) >= 100:
                    break
            complete = next_offset == len(events)
            # Queue before advancing the canonical cursor. Unknown deliveries
            # remain durable even if restart happens immediately after checkpoint.
            queue.db.execute("BEGIN IMMEDIATE")
            try:
                if not queue.can_enqueue(selections):
                    queue.db.execute("ROLLBACK")
                    break
                for selection in selections:
                    queue.enqueue(selection)
                if complete:
                    queue.db.execute(
                        "INSERT INTO cursor VALUES(1,?,?) ON CONFLICT(id) DO "
                        "UPDATE SET block=excluded.block,hash=excluded.hash",
                        (block, at),
                    )
                    queue.db.execute("DELETE FROM block_progress WHERE id=1")
                else:
                    queue.db.execute(
                        "INSERT INTO block_progress VALUES(1,?,?,?) ON CONFLICT(id) DO "
                        "UPDATE SET block=excluded.block,hash=excluded.hash,"
                        "event_offset=excluded.event_offset",
                        (block, at, next_offset),
                    )
                queue.db.execute("COMMIT")
            except BaseException:
                queue.db.execute("ROLLBACK")
                raise
            if not complete:
                break
        delivered = 0
        rows = queue.db.execute(
            (
                "SELECT id,body FROM pending WHERE receipt_id IS NULL "
                "ORDER BY rowid LIMIT ?"
            ),
            (config.max_deliveries,),
        ).fetchall()
        for identity, body in rows:
            selection = json.loads(body)
            result = mcp.call(
                "record_treasury_receipt",
                {**selection, "confirmation": "INGEST VERIFIED TREASURY RECEIPT"},
            )
            if (
                result.get("status") != "chain_finalized"
                or result.get("provider_credit_status") != "not_proven"
                or result.get("stage") != selection["stage"]
                or result.get("policy_digest") != policy.digest
                or result.get("bucket_id") != selection["bucket_id"]
                or result.get("epoch_index") != selection["epoch_index"]
                or result.get("source_block") != selection.get("source_block")
                or any(
                    result.get(field) != selection[field]
                    for field in (
                        "block",
                        "block_hash",
                        "extrinsic_index",
                        "extrinsic_hash",
                    )
                )
                or result.get("amount_atomic") != str(selection["amount_atomic"])
                or len(result.get("receipt_id", "")) != 64
            ):
                raise ValueError(
                    "verified public MCP acknowledgment differs from queued selection"
                )
            try:
                bytes.fromhex(result["receipt_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid verified receipt digest") from exc
            queue.db.execute(
                "UPDATE pending SET receipt_id=? WHERE id=? AND receipt_id IS NULL",
                (result["receipt_id"], identity),
            )
            delivered += 1
        if config.selector_handoff is not None:
            acknowledge_pages(queue, config.selector_handoff)
        return {
            "status": "observed",
            "delivered": delivered,
            "pending": queue.db.execute(
                "SELECT count(*) FROM pending WHERE receipt_id IS NULL"
            ).fetchone()[0],
            "authority": "none",
        }
    finally:
        queue.close()
