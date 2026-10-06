"""Durable service collector orchestration, separate from vendor purchases.

Only the chain adapter supplies finalized observations and signed transactions.
A signed transaction is persisted before dispatch, and uncertainty never causes
another signature. No import accesses a wallet, chain, secret or environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from ditto.treasury.service_allocation import (
    ServiceDestination,
    ServiceDistribution,
    plan_service_distribution,
)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CollectorPolicy:
    genesis_hash: str
    runtime_code_hash: str
    collector_coldkey: str
    collector_hotkey: str
    registration_delegate: str
    transfer_delegate: str
    revision: int
    start_block: int
    destinations: tuple[ServiceDestination, ...]
    max_registration_burn_rao: int
    registration_budget_rao: int
    max_fee_rao: int
    fee_reserve_rao: int
    recovery_cooldown_blocks: int
    distribution_interval_blocks: int
    max_distribution_rao: int
    gcp_project: str
    registration_service_account: str
    transfer_service_account: str
    registration_secret_version: int
    transfer_secret_version: int
    enabled: bool = False
    netuid: int = 118

    def __post_init__(self) -> None:
        for name in ("genesis_hash", "runtime_code_hash"):
            value = getattr(self, name)
            if len(value) != 66 or not value.startswith("0x"):
                raise ValueError(f"invalid {name}")
            bytes.fromhex(value[2:])
        keys = (
            self.collector_coldkey,
            self.collector_hotkey,
            self.registration_delegate,
            self.transfer_delegate,
        )
        if len(set(keys)) != 4 or any(not k for k in keys):
            raise ValueError("four distinct collector and delegate identities required")
        if self.netuid != 118 or type(self.enabled) is not bool:
            raise ValueError("only SN118 and explicit boolean activation supported")
        for name in (
            "revision",
            "start_block",
            "max_registration_burn_rao",
            "registration_budget_rao",
            "max_fee_rao",
            "fee_reserve_rao",
            "recovery_cooldown_blocks",
            "distribution_interval_blocks",
            "max_distribution_rao",
            "registration_secret_version",
            "transfer_secret_version",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError(f"positive bounded integer required: {name}")
        if (
            not self.gcp_project
            or not self.registration_service_account.endswith(
                ".iam.gserviceaccount.com"
            )
            or not self.transfer_service_account.endswith(".iam.gserviceaccount.com")
            or self.registration_service_account == self.transfer_service_account
        ):
            raise ValueError("distinct dedicated GCP signer principals required")
        if self.fee_reserve_rao < self.max_fee_rao:
            raise ValueError("fee reserve must cover maximum fee")
        if sum(d.allocation_bps for d in self.destinations) != 1000:
            raise ValueError("combined service pool must be exactly 1000 bps")
        plan_service_distribution(
            attributed_alpha_rao=1,
            available_alpha_rao=1,
            collector_coldkey=self.collector_coldkey,
            destinations=self.destinations,
        )
        if any(d.holding_coldkey in keys for d in self.destinations):
            raise ValueError("holding wallets must be distinct from signer roles")

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True)
class Observation:
    block: int
    block_hash: str
    uid: int | None
    burn_rao: int
    collector_free_rao: int
    delegate_free_rao: int
    alpha_rao: int


@dataclass(frozen=True)
class TransferCanary:
    """Stricter operator ceiling; one new claim, persisted in existing history."""

    max_alpha_rao: int
    after_operation: int

    def __post_init__(self) -> None:
        if (
            type(self.max_alpha_rao) is not int
            or not 0 < self.max_alpha_rao < 2**63
            or type(self.after_operation) is not int
            or not 0 <= self.after_operation < 2**63
        ):
            raise ValueError("invalid transfer canary bounds")


def transfer_canary(
    journal: CollectorJournal,
    policy: CollectorPolicy,
    role: str,
    requested: TransferCanary | None,
) -> TransferCanary | None:
    """Keep a canary armed even if a later invocation omits CLI flags."""
    db = journal.db
    rows = db.execute(
        "SELECT payload FROM events WHERE event='transfer_canary_armed'"
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("ambiguous transfer canary history")
    if rows:
        body = json.loads(rows[0][0])
        if set(body) != {"policy", "max_alpha_rao", "after_operation"}:
            raise ValueError("invalid transfer canary history")
        if body["policy"] != policy.digest:
            raise ValueError("transfer canary policy changed")
        existing = TransferCanary(body["max_alpha_rao"], body["after_operation"])
        if requested is not None and requested != existing:
            raise ValueError("transfer canary cannot be reset or widened")
        requested = existing
    if requested is None:
        return None
    if role != "transfer" or requested.max_alpha_rao > policy.max_distribution_rao:
        raise ValueError("canary requires transfer role within signed ceiling")
    if not rows:
        maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM operations").fetchone()[0]
        if (
            maximum != requested.after_operation
            or db.execute(
                "SELECT 1 FROM operations WHERE state='dispatching' LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("canary baseline must equal quiesced current journal")
        journal.event(
            "transfer_canary_armed", {"policy": policy.digest, **asdict(requested)}
        )
    claimed = db.execute(
        "SELECT role,amount FROM operations WHERE id>?", (requested.after_operation,)
    ).fetchall()
    if len(claimed) > 1 or any(
        r[0] != "transfer" or not 0 < r[1] <= requested.max_alpha_rao for r in claimed
    ):
        raise ValueError("transfer canary claim exceeds approved bound")
    return requested


@dataclass(frozen=True)
class SignedOperation:
    encoded: str
    extrinsic_hash: str
    start_block: int
    expires_block: int
    fee_rao: int


@dataclass(frozen=True)
class FinalizedEarnings:
    amount_rao: int
    block_hash: str
    event_digest: str


@dataclass(frozen=True)
class Settlement:
    status: str  # finalized, failed, expired, pending
    block: int | None = None
    block_hash: str | None = None
    uid: int | None = None
    scanned_through: int | None = None
    extrinsic_index: int | None = None
    extrinsic_hash: str | None = None


class CollectorChain(Protocol):
    def observe(self, policy: CollectorPolicy, role: str) -> Observation: ...
    def earnings(
        self, policy: CollectorPolicy, block: int
    ) -> FinalizedEarnings | None: ...
    def prepare(
        self, policy: CollectorPolicy, role: str, call: dict, observation: Observation
    ) -> SignedOperation: ...
    def broadcast(self, encoded: str) -> None: ...
    def reconcile(
        self, policy: CollectorPolicy, operation: dict, observation: Observation
    ) -> Settlement: ...


class CollectorJournal:
    """One private durable database per isolated signer; no shared writable DB."""

    def __init__(
        self,
        path: Path,
        policy: CollectorPolicy,
        role: str,
        *,
        initialize: bool = False,
    ):
        if role not in {"registration", "transfer"}:
            raise ValueError("unknown signer role")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = path.parent.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_mode & 0o077
            or directory.st_uid != os.geteuid()
        ):
            raise ValueError("journal directory must be private and signer-owned")
        flags = os.O_RDWR | os.O_NOFOLLOW
        if initialize:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
            ):
                raise ValueError("journal must be a private regular file")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        if not initialize:
            row = self.db.execute("SELECT digest,role FROM pin").fetchone()
            if row is None or tuple(row) != (policy.digest, role):
                self.db.close()
                raise ValueError("existing journal pin unavailable or changed")
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pin (digest TEXT PRIMARY KEY, role TEXT);
            CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY, block INTEGER);
            CREATE TABLE IF NOT EXISTS earnings (
                block INTEGER PRIMARY KEY, amount INTEGER NOT NULL,
                block_hash TEXT NOT NULL, event_digest TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY, state TEXT NOT NULL, role TEXT NOT NULL,
                amount INTEGER NOT NULL, source_block INTEGER,
                bucket TEXT, destination TEXT, call_json TEXT NOT NULL,
                signed_json TEXT NOT NULL, at_block INTEGER NOT NULL,
                settlement_json TEXT, reconciled_through INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, event TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM pin").fetchone()
            if row and (row["digest"] != policy.digest or row["role"] != role):
                raise ValueError(
                    "journal policy/role is immutable; reconcile before migration"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO pin VALUES (?,?)", (policy.digest, role)
            )
            self.db.execute(
                "INSERT OR IGNORE INTO cursor VALUES (1,?)", (policy.start_block - 1,)
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, name: str, payload: object) -> None:
        self.db.execute(
            "INSERT INTO events(event,payload) VALUES (?,?)", (name, canonical(payload))
        )

    def close(self) -> None:
        self.db.close()


def remaining_distribution(
    parts: tuple[ServiceDistribution, ...], operations: list[sqlite3.Row]
) -> tuple[ServiceDistribution, ...]:
    """Conserve each receipt's bucket entitlement after finalized partial legs."""
    expected = {part.bucket_id: part for part in parts}
    paid = dict.fromkeys(expected, 0)
    for operation in operations:
        bucket = operation["bucket"]
        amount = operation["amount"]
        if (
            bucket not in expected
            or operation["role"] != "transfer"
            or operation["state"] != "finalized"
            or operation["destination"] != expected[bucket].holding_coldkey
            or type(amount) is not int
            or amount <= 0
        ):
            raise ValueError("invalid finalized receipt distribution history")
        paid[bucket] += amount
        if paid[bucket] > expected[bucket].alpha_rao:
            raise ValueError("finalized distribution exceeds receipt bucket")
    return tuple(
        replace(part, alpha_rao=part.alpha_rao - paid[part.bucket_id])
        for part in parts
        if part.alpha_rao > paid[part.bucket_id]
    )


def tick(
    journal: CollectorJournal,
    policy: CollectorPolicy,
    chain: CollectorChain,
    role: str,
    *,
    canary: TransferCanary | None = None,
) -> str:
    """Run one bounded recovery/distribution step. Called periodically by systemd.

    DB lock covers observation, preparation and durable reservation. It does not
    cover broadcast. Another process sees dispatching and can only reconcile.
    """
    if not policy.enabled:
        return "disabled"
    db = journal.db
    db.execute("BEGIN IMMEDIATE")
    signed: SignedOperation | None = None
    try:
        if db.execute("SELECT digest,role FROM pin").fetchone()[:] != (
            policy.digest,
            role,
        ):
            raise ValueError("policy or role changed")
        canary = transfer_canary(journal, policy, role, canary)
        if (
            canary is not None
            and db.execute(
                "SELECT 1 FROM operations WHERE id>? AND state!='dispatching' LIMIT 1",
                (canary.after_operation,),
            ).fetchone()
        ):
            db.execute("COMMIT")
            return "canary_spent"
        observed = chain.observe(policy, role)
        pending = db.execute(
            "SELECT * FROM operations WHERE state='dispatching'"
        ).fetchone()
        if pending:
            result = chain.reconcile(policy, dict(pending), observed)
            if result.status not in {"finalized", "failed", "expired", "pending"}:
                raise ValueError("invalid settlement")
            if result.status == "pending":
                if result.scanned_through is not None:
                    if (
                        not pending["reconciled_through"]
                        <= result.scanned_through
                        <= observed.block
                    ):
                        raise ValueError("invalid reconciliation progress")
                    db.execute(
                        "UPDATE operations SET reconciled_through=? WHERE id=?",
                        (result.scanned_through, pending["id"]),
                    )
                db.execute("COMMIT")
                return "pending"
            db.execute(
                "UPDATE operations SET state=?,settlement_json=? WHERE id=?",
                (result.status, canonical(asdict(result)), pending["id"]),
            )
            journal.event("settlement", {"operation": pending["id"], **asdict(result)})
            if pending["role"] == "registration" and result.status == "finalized":
                if result.uid is None:
                    raise ValueError("registration receipt lacks finalized UID binding")
                journal.event("collector_uid_rebound", asdict(result))
            db.execute("COMMIT")
            return result.status
        if role == "registration":
            if observed.uid is not None:
                db.execute("COMMIT")
                return "registered"
            last = db.execute("SELECT MAX(at_block) FROM operations").fetchone()[0]
            if (
                last is not None
                and observed.block - last < policy.recovery_cooldown_blocks
            ):
                db.execute("COMMIT")
                return "cooldown"
            # Conservatively charge every reserved attempt, even failed/expired.
            spent = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM operations"
            ).fetchone()[0]
            amount = policy.max_registration_burn_rao
            if (
                observed.burn_rao > amount
                or amount + policy.max_fee_rao + spent > policy.registration_budget_rao
            ):
                raise ValueError("registration burn or lifetime budget exceeded")
            if observed.collector_free_rao < amount + policy.fee_reserve_rao:
                raise ValueError("collector registration reserve insufficient")
            call = {
                "module": "SubtensorModule",
                "function": "register_limit",
                "params": {
                    "netuid": 118,
                    "hotkey": policy.collector_hotkey,
                    "limit_price": amount,
                },
            }
            bucket = destination = source_block = None
        else:
            if observed.uid is None:
                raise ValueError("collector absent; service transfers halted")
            row = db.execute(
                """SELECT * FROM earnings e WHERE NOT EXISTS
                (SELECT 1 FROM operations o WHERE o.source_block=e.block
                 AND o.state IN ('dispatching','failed','expired'))
                AND e.completed=0 AND e.block+?<=? ORDER BY e.block LIMIT 1""",
                (policy.distribution_interval_blocks, observed.block),
            ).fetchone()
            if not row:
                cursor = db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0]
                # Bound archive/RPC work to 32 finalized blocks per invocation.
                for block in range(cursor + 1, min(observed.block, cursor + 32) + 1):
                    earned = chain.earnings(policy, block)
                    if earned is not None:
                        if (
                            not isinstance(earned, FinalizedEarnings)
                            or type(earned.amount_rao) is not int
                            or not 0 < earned.amount_rao < 2**63
                        ):
                            raise ValueError("invalid finalized emission")
                        db.execute(
                            "INSERT INTO earnings"
                            "(block,amount,block_hash,event_digest) "
                            "VALUES (?,?,?,?)",
                            (
                                block,
                                earned.amount_rao,
                                earned.block_hash,
                                earned.event_digest,
                            ),
                        )
                        journal.event(
                            "emission_attributed",
                            {"block": block, "policy": policy.digest, **asdict(earned)},
                        )
                    db.execute("UPDATE cursor SET block=? WHERE id=1", (block,))
                # Commit independently proved history without using the now
                # stale observation to sign. A following tick observes a fresh
                # finalized head and prioritizes this durable mature receipt.
                db.execute("COMMIT")
                return "observing"
            previous_batch = db.execute(
                "SELECT MAX(at_block) FROM operations WHERE source_block!=?",
                (row["block"],),
            ).fetchone()[0]
            if (
                previous_batch is not None
                and observed.block - previous_batch
                < policy.distribution_interval_blocks
            ):
                db.execute("COMMIT")
                return "interval"
            if row["amount"] > policy.max_distribution_rao:
                raise ValueError(
                    "distribution ceiling exceeded; no partial unreviewed split"
                )
            parts = plan_service_distribution(
                attributed_alpha_rao=row["amount"],
                available_alpha_rao=row["amount"],
                collector_coldkey=policy.collector_coldkey,
                destinations=policy.destinations,
            )
            remaining = remaining_distribution(
                parts,
                db.execute(
                    "SELECT bucket,amount,destination,role,state FROM operations "
                    "WHERE source_block=?",
                    (row["block"],),
                ).fetchall(),
            )
            if sum(part.alpha_rao for part in remaining) > observed.alpha_rao:
                raise ValueError("remaining attributed earnings exceed finalized stake")
            part = next(iter(remaining), None)
            if part is None:
                # Earnings remain as audit records; complete batches skip onward.
                db.execute(
                    "UPDATE earnings SET completed=1 WHERE block=?", (row["block"],)
                )
                journal.event(
                    "distribution_complete",
                    {"source_block": row["block"], "amount": row["amount"]},
                )
                db.execute("COMMIT")
                return "distributed"
            amount, bucket, destination, source_block = (
                part.alpha_rao,
                part.bucket_id,
                part.holding_coldkey,
                row["block"],
            )
            if canary is not None and amount > canary.max_alpha_rao:
                # Preserve the full receipt and all other bucket entitlements.
                # The persisted one-claim guard still stops after this leg;
                # omitting flags never enables distribution of its remainder.
                amount = canary.max_alpha_rao
            if canary is not None:
                journal.event(
                    "canary_receipt_reserved",
                    {
                        "source_block": row["block"],
                        "source_hash": row["block_hash"],
                        "source_event_digest": row["event_digest"],
                        "source_amount": row["amount"],
                        "bucket": bucket,
                        "bucket_remaining_before": part.alpha_rao,
                        "canary_amount": amount,
                        "receipt_remaining_after": sum(
                            part.alpha_rao for part in remaining
                        )
                        - amount,
                    },
                )
            call = {
                "module": "SubtensorModule",
                "function": "transfer_stake",
                "params": {
                    "destination_coldkey": destination,
                    "hotkey": policy.collector_hotkey,
                    "origin_netuid": 118,
                    "destination_netuid": 118,
                    "alpha_amount": amount,
                },
            }
        if observed.delegate_free_rao < policy.max_fee_rao + policy.fee_reserve_rao:
            raise ValueError("delegate fee reserve insufficient")
        signed = chain.prepare(policy, role, call, observed)
        if (
            type(signed.fee_rao) is not int
            or not 0 <= signed.fee_rao <= policy.max_fee_rao
        ):
            raise ValueError("quoted fee exceeds ceiling")
        if (
            signed.start_block != observed.block
            or not observed.block < signed.expires_block <= observed.block + 128
        ):
            raise ValueError("invalid transaction mortality")
        db.execute(
            """INSERT INTO operations(state,role,amount,source_block,bucket,
            destination,call_json,signed_json,at_block)
            VALUES ('dispatching',?,?,?,?,?,?,?,?)""",
            (
                role,
                amount + policy.max_fee_rao if role == "registration" else amount,
                source_block,
                bucket,
                destination,
                canonical(call),
                canonical(asdict(signed)),
                observed.block,
            ),
        )
        journal.event(
            "dispatch_reserved",
            {"hash": signed.extrinsic_hash, "policy": policy.digest, "role": role},
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    # Never sign/send again on an RPC error. The persisted exact hash is scanned
    # by the next invocation until finalized or provably expired.
    chain.broadcast(signed.encoded)
    return "dispatching"
