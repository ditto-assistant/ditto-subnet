"""Durable, fail-closed journal for one treasury top-up at a time.

The database is local to the isolated signer. A dispatch claim is committed
*before* a network call. An interrupted call is ambiguous and never retried
automatically. The append-only event chain makes operator repairs visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ditto.treasury.preflight import TopUpBounds, TopUpIntent, preflight

GENESIS = "0" * 64
LEGS = {
    "tao": ("unstake", "deposit_tao"),
    "gm_alpha": ("unstake", "stake_gm", "deposit_gm"),
}
TERMINAL = {"reconciled", "cancelled"}


@dataclass(frozen=True)
class PaymentPlan:
    intent: TopUpIntent
    destination_coldkey: str
    treasury_hotkey: str
    gm_hotkey: str
    gm_account_ref: str
    operator: str
    instructions_observed_at: datetime
    gm_balance_before_nano_usd: int
    min_tao_proceeds_rao: int
    min_gm_alpha_rao: int
    max_slippage_bps: int


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value.astimezone(UTC).isoformat()


class TreasuryStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("treasury database may not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("treasury database must be a private regular file")
        finally:
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS control (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                paused INTEGER NOT NULL CHECK (paused IN (0,1)),
                reason TEXT NOT NULL
            );
            INSERT OR IGNORE INTO control VALUES (1,1,'initial fail-closed pause');
            CREATE TABLE IF NOT EXISTS plans (
                idempotency_key TEXT PRIMARY KEY,
                day TEXT NOT NULL,
                route TEXT NOT NULL CHECK (route IN ('tao','gm_alpha')),
                policy_revision INTEGER NOT NULL,
                source_alpha_rao INTEGER NOT NULL,
                reserved_tao_rao INTEGER NOT NULL,
                plan_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                leg_index INTEGER NOT NULL DEFAULT 0,
                amount_for_next_leg_rao INTEGER,
                instructions_checked_at TEXT NOT NULL,
                quote_checked_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allocations (
                epoch INTEGER PRIMARY KEY,
                policy_revision INTEGER NOT NULL,
                source_block_hash TEXT NOT NULL,
                gm_alpha_rao INTEGER NOT NULL,
                maintenance_alpha_rao INTEGER NOT NULL,
                operator TEXT NOT NULL,
                reviewer TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_key TEXT,
                kind TEXT NOT NULL,
                body_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                hash TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (plan_key) REFERENCES plans(idempotency_key)
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
              BEGIN SELECT RAISE(ABORT, 'treasury events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
              BEGIN SELECT RAISE(ABORT, 'treasury events are append-only'); END;
            """
        )
        return connection

    @contextmanager
    def _db(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        key: str | None,
        kind: str,
        body: dict[str, Any],
        now: datetime,
    ) -> str:
        row = db.execute(
            "SELECT hash FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = row["hash"] if row else GENESIS
        recorded_at = _timestamp(now)
        canonical = _canonical(body)
        digest = hashlib.sha256(
            _canonical([key, kind, canonical, previous, recorded_at]).encode()
        ).hexdigest()
        db.execute(
            "INSERT INTO events"
            "(plan_key,kind,body_json,previous_hash,hash,recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (key, kind, canonical, previous, digest, recorded_at),
        )
        return digest

    def verify(self) -> int:
        with self._db() as db:
            previous = GENESIS
            count = 0
            for row in db.execute("SELECT * FROM events ORDER BY sequence"):
                expected = hashlib.sha256(
                    _canonical(
                        [
                            row["plan_key"],
                            row["kind"],
                            row["body_json"],
                            previous,
                            row["recorded_at"],
                        ]
                    ).encode()
                ).hexdigest()
                if (
                    row["sequence"] != count + 1
                    or row["previous_hash"] != previous
                    or row["hash"] != expected
                ):
                    raise ValueError(f"treasury journal breaks at event {count + 1}")
                previous = expected
                count += 1
            return count

    def status(self) -> dict[str, Any]:
        self.verify()
        with self._db() as db:
            control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            plans = [
                dict(row)
                for row in db.execute(
                    "SELECT idempotency_key,day,route,state,leg_index,plan_hash"
                    " FROM plans ORDER BY created_at DESC"
                )
            ]
            return {
                "paused": bool(control["paused"]),
                "reason": control["reason"],
                "plans": plans,
            }

    def plan(self, key: str) -> dict[str, Any]:
        self.verify()
        with self._db() as db:
            row = db.execute(
                "SELECT plan_json FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown treasury plan")
            return json.loads(row["plan_json"])

    def record_allocation(
        self,
        *,
        epoch: int,
        policy_revision: int,
        source_block_hash: str,
        gm_alpha_rao: int,
        maintenance_alpha_rao: int,
        operator: str,
        reviewer: str,
        now: datetime,
    ) -> None:
        if epoch < 0 or policy_revision <= 0:
            raise ValueError("finalized epoch and policy revision are required")
        if not re.fullmatch(r"0x[0-9a-f]{64}", source_block_hash):
            raise ValueError("finalized source block hash is required")
        if (
            gm_alpha_rao < 0
            or maintenance_alpha_rao < 0
            or not (gm_alpha_rao or maintenance_alpha_rao)
        ):
            raise ValueError("nonnegative distinct allocations are required")
        if not operator or not reviewer or reviewer == operator:
            raise ValueError("independent allocation reviewer is required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO allocations VALUES (?,?,?,?,?,?,?)",
                (
                    epoch,
                    policy_revision,
                    source_block_hash,
                    gm_alpha_rao,
                    maintenance_alpha_rao,
                    operator,
                    reviewer,
                ),
            )
            self._event(
                db,
                None,
                "allocation",
                {
                    "epoch": epoch,
                    "policy_revision": policy_revision,
                    "source_block_hash": source_block_hash,
                    "gm_alpha_rao": gm_alpha_rao,
                    "maintenance_alpha_rao": maintenance_alpha_rao,
                    "operator": operator,
                    "reviewer": reviewer,
                },
                now,
            )

    def set_pause(
        self,
        paused: bool,
        *,
        operator: str,
        reason: str,
        now: datetime,
        reviewer: str | None = None,
    ) -> None:
        if not operator or len(reason.strip()) < 12:
            raise ValueError("operator and a specific reason are required")
        if not paused:
            if not reviewer or reviewer == operator:
                raise ValueError("independent reviewer is required to unpause")
            self.verify()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not paused:
                unresolved = db.execute(
                    "SELECT COUNT(*) FROM plans"
                    " WHERE state IN ('dispatching','awaiting_gm')"
                ).fetchone()[0]
                if unresolved:
                    raise ValueError("unresolved plans block unpause")
            db.execute(
                "UPDATE control SET paused=?,reason=? WHERE singleton=1",
                (int(paused), reason),
            )
            self._event(
                db,
                None,
                "pause" if paused else "unpause",
                {"operator": operator, "reviewer": reviewer, "reason": reason},
                now,
            )

    def create_plan(
        self, plan: PaymentPlan, bounds: TopUpBounds, *, now: datetime
    ) -> str:
        intent = plan.intent
        if (
            not plan.operator
            or not plan.destination_coldkey
            or not plan.treasury_hotkey
            or not plan.gm_account_ref
        ):
            raise ValueError("operator and reviewed chain identities are required")
        if intent.route == "gm_alpha" and not plan.gm_hotkey:
            raise ValueError("GM alpha route needs the exact linked hotkey")
        if plan.gm_balance_before_nano_usd < 0:
            raise ValueError("GM credit balance must be nonnegative")
        if not 0 < plan.min_tao_proceeds_rao <= intent.tao_value_rao:
            raise ValueError("minimum TAO proceeds must fit the reserved quote")
        if intent.route == "gm_alpha" and plan.min_gm_alpha_rao <= 0:
            raise ValueError("minimum GM alpha output is required")
        if not 0 <= plan.max_slippage_bps <= bounds.max_slippage_bps:
            raise ValueError("signer price tolerance exceeds policy")
        if plan.instructions_observed_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("instruction and current times need a timezone")
        if (
            not timedelta(0)
            <= now.astimezone(UTC) - plan.instructions_observed_at.astimezone(UTC)
            <= timedelta(minutes=30)
        ):
            raise ValueError("GM payment instructions are stale")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", intent.idempotency_key):
            raise ValueError("invalid idempotency key")
        body = asdict(plan)
        body["intent"]["quoted_at"] = _timestamp(intent.quoted_at)
        body["instructions_observed_at"] = _timestamp(plan.instructions_observed_at)
        canonical = _canonical(body)
        plan_hash = hashlib.sha256(canonical.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT paused FROM control WHERE singleton=1").fetchone()[0]:
                raise ValueError("treasury execution is paused")
            existing = db.execute(
                "SELECT plan_hash FROM plans WHERE idempotency_key=?",
                (intent.idempotency_key,),
            ).fetchone()
            if existing:
                if existing["plan_hash"] != plan_hash:
                    raise ValueError("idempotency key reused with a different plan")
                return plan_hash
            if db.execute(
                "SELECT COUNT(*) FROM plans"
                " WHERE state NOT IN ('reconciled','cancelled')"
            ).fetchone()[0]:
                raise ValueError("another treasury payment is unresolved")
            day = now.astimezone(UTC).date().isoformat()
            spent = db.execute(
                "SELECT COALESCE(SUM(reserved_tao_rao),0) FROM plans"
                " WHERE day=? AND state!='cancelled'",
                (day,),
            ).fetchone()[0]
            preflight(intent, replace(bounds, daily_spent_rao=spent), now=now)
            allocated = db.execute(
                "SELECT COALESCE(SUM(gm_alpha_rao),0) FROM allocations"
                " WHERE policy_revision<=?",
                (intent.policy_revision,),
            ).fetchone()[0]
            reserved_source = db.execute(
                "SELECT COALESCE(SUM(source_alpha_rao),0) FROM plans"
                " WHERE state!='cancelled'"
            ).fetchone()[0]
            if intent.source_alpha_rao > allocated - reserved_source:
                raise ValueError("GM budget lacks finalized allocated alpha")
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.idempotency_key,
                    day,
                    intent.route,
                    intent.policy_revision,
                    intent.source_alpha_rao,
                    intent.tao_value_rao,
                    canonical,
                    plan_hash,
                    "planned",
                    0,
                    None,
                    _timestamp(plan.instructions_observed_at),
                    _timestamp(intent.quoted_at),
                    _timestamp(now),
                ),
            )
            self._event(
                db,
                intent.idempotency_key,
                "planned",
                {"plan_hash": plan_hash, "operator": plan.operator},
                now,
            )
        return plan_hash

    def approve(
        self, key: str, *, reviewer: str, expected_plan_hash: str, now: datetime
    ) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "planned"
                or row["plan_hash"] != expected_plan_hash
            ):
                raise ValueError("exact pending plan hash is required")
            plan = json.loads(row["plan_json"])
            if not reviewer or reviewer == plan["operator"]:
                raise ValueError("independent reviewer is required")
            observed = datetime.fromisoformat(plan["instructions_observed_at"])
            if (
                not timedelta(0)
                <= now.astimezone(UTC) - observed
                <= timedelta(minutes=30)
            ):
                raise ValueError("GM payment instructions expired")
            db.execute(
                "UPDATE plans SET state='approved' WHERE idempotency_key=?", (key,)
            )
            self._event(
                db,
                key,
                "approved",
                {"reviewer": reviewer, "plan_hash": expected_plan_hash},
                now,
            )

    def approve_next_leg(
        self,
        key: str,
        *,
        reviewer: str,
        expected_plan_hash: str,
        instructions_sha256: str,
        quote_block_hash: str,
        quote_observed_at: datetime,
        now: datetime,
    ) -> None:
        if not re.fullmatch(r"0x[0-9a-f]{64}", quote_block_hash):
            raise ValueError("fresh finalized quote hash is required")
        if quote_observed_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("quote time requires a timezone")
        if not timedelta(0) <= now - quote_observed_at <= timedelta(seconds=120):
            raise ValueError("next-leg quote is stale")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "awaiting_review"
                or row["plan_hash"] != expected_plan_hash
            ):
                raise ValueError("exact next-leg plan is required")
            plan = json.loads(row["plan_json"])
            if not reviewer or reviewer == plan["operator"]:
                raise ValueError("independent reviewer is required")
            if instructions_sha256 != plan["intent"]["payment_instructions_sha256"]:
                raise ValueError("GM payment instructions changed")
            db.execute(
                "UPDATE plans SET state='approved',instructions_checked_at=?,"
                "quote_checked_at=?"
                " WHERE idempotency_key=?",
                (_timestamp(now), _timestamp(quote_observed_at), key),
            )
            self._event(
                db,
                key,
                "next_leg_approved",
                {
                    "reviewer": reviewer,
                    "leg": LEGS[row["route"]][row["leg_index"]],
                    "quote_block_hash": quote_block_hash,
                    "instructions_sha256": instructions_sha256,
                },
                now,
            )

    def claim_leg(self, key: str, *, now: datetime) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT paused FROM control WHERE singleton=1").fetchone()[0]:
                raise ValueError("treasury execution is paused")
            row = db.execute(
                "SELECT * FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None or row["state"] != "approved":
                raise ValueError("leg is not approved or prior dispatch is unresolved")
            plan = json.loads(row["plan_json"])
            leg = LEGS[row["route"]][row["leg_index"]]
            if (
                not timedelta(0)
                <= now.astimezone(UTC)
                - datetime.fromisoformat(row["instructions_checked_at"])
                <= timedelta(minutes=30)
            ):
                raise ValueError("GM payment instructions expired")
            quote_age = now.astimezone(UTC) - datetime.fromisoformat(
                row["quote_checked_at"]
            )
            if not timedelta(0) <= quote_age <= timedelta(seconds=120):
                raise ValueError("approved leg quote expired")
            amount = (
                row["source_alpha_rao"]
                if row["leg_index"] == 0
                else row["amount_for_next_leg_rao"]
            )
            if amount is None or amount <= 0:
                raise ValueError("next leg amount has not reconciled")
            db.execute(
                "UPDATE plans SET state='dispatching' WHERE idempotency_key=?", (key,)
            )
            self._event(
                db, key, "dispatch_started", {"leg": leg, "amount_rao": amount}, now
            )
            return {"leg": leg, "amount_rao": amount, "plan": plan}

    def finalize_leg(
        self,
        key: str,
        *,
        leg: str,
        extrinsic_hash: str,
        block_hash: str,
        next_amount_rao: int | None,
        now: datetime,
    ) -> None:
        if not re.fullmatch(r"0x[0-9a-f]{64}", extrinsic_hash) or not re.fullmatch(
            r"0x[0-9a-f]{64}", block_hash
        ):
            raise ValueError("finalized extrinsic and block hashes are required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "dispatching"
                or LEGS[row["route"]][row["leg_index"]] != leg
            ):
                raise ValueError("no matching in-flight leg")
            final = row["leg_index"] + 1 == len(LEGS[row["route"]])
            if not final and (next_amount_rao is None or next_amount_rao <= 0):
                raise ValueError("reconciled next-leg amount is required")
            if final and next_amount_rao is not None:
                raise ValueError("deposit leg cannot produce another payment")
            if (
                leg == "unstake"
                and next_amount_rao is not None
                and next_amount_rao > row["reserved_tao_rao"]
            ):
                raise ValueError("unstake proceeds exceed reserved top-up")
            state = "awaiting_gm" if final else "awaiting_review"
            db.execute(
                "UPDATE plans SET state=?,leg_index=leg_index+1,"
                "amount_for_next_leg_rao=? WHERE idempotency_key=?",
                (state, next_amount_rao, key),
            )
            self._event(
                db,
                key,
                "leg_finalized",
                {
                    "leg": leg,
                    "extrinsic_hash": extrinsic_hash,
                    "block_hash": block_hash,
                    "next_amount_rao": next_amount_rao,
                },
                now,
            )

    def reconcile_gm(
        self,
        key: str,
        *,
        before_nano_usd: int,
        after_nano_usd: int,
        deposit_nano_usd: int,
        intervening_usage_nano_usd: int,
        deposit_reference: str,
        reviewer: str,
        now: datetime,
    ) -> None:
        if (
            not deposit_reference
            or not reviewer
            or deposit_nano_usd <= 0
            or intervening_usage_nano_usd < 0
            or after_nano_usd < 0
            or after_nano_usd
            != before_nano_usd + deposit_nano_usd - intervening_usage_nano_usd
        ):
            raise ValueError("GM deposit, usage, and final balance must reconcile")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None or row["state"] != "awaiting_gm":
                raise ValueError("finalized deposit is required")
            plan = json.loads(row["plan_json"])
            if (
                before_nano_usd != plan["gm_balance_before_nano_usd"]
                or reviewer == plan["operator"]
            ):
                raise ValueError("independent matching credit observation is required")
            db.execute(
                "UPDATE plans SET state='reconciled' WHERE idempotency_key=?", (key,)
            )
            self._event(
                db,
                key,
                "gm_reconciled",
                {
                    "before_nano_usd": before_nano_usd,
                    "after_nano_usd": after_nano_usd,
                    "deposit_nano_usd": deposit_nano_usd,
                    "intervening_usage_nano_usd": intervening_usage_nano_usd,
                    "deposit_reference": deposit_reference,
                    "reviewer": reviewer,
                },
                now,
            )

    def cancel(self, key: str, *, operator: str, reason: str, now: datetime) -> None:
        if not operator or len(reason.strip()) < 12:
            raise ValueError("operator and specific reason are required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM plans WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None or row["state"] != "planned":
                raise ValueError(
                    "only an undispatched, unapproved plan may be cancelled"
                )
            db.execute(
                "UPDATE plans SET state='cancelled' WHERE idempotency_key=?", (key,)
            )
            self._event(
                db, key, "cancelled", {"operator": operator, "reason": reason}, now
            )
