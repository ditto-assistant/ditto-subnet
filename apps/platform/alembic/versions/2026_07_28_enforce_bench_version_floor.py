"""make a retired benchmark era unscoreable in the database, not in a setting

Revision ID: d4b8e6c1a205
Revises: 45ef71514f21
Create Date: 2026-07-28

v2-v5 were already dead by construction: three forward-only guards
(``benchmark_rollout.py``, ``admin_benchmark_rollout.py`` and the
``benchmark_rollout_forward`` CHECK) mean no rollout can ever walk the active
version backwards. v6 was dead only by CONFIGURATION, and that is the gap this
closes.

Two live paths admitted v6 the day this was written. The source-backfill lane
re-issued an existing unexpired v6 lease before it reached its own retired-era
gate, and ``request_job`` deliberately resurrects the activated v7 rollout to
feed that lane -- and that row's ``from_version`` is 6. Separately,
``allow_retired_era_backfill`` was an MCP-exposed runtime setting whose own
docstring advertised that flipping it restored the old post-activation
behaviour "without a deploy". One Backroom write re-opened a retired era. Both
paths are removed in the application, but an application guard is a guard that
the next refactor can move above the check again -- which is exactly how the
resume path ended up above the gate. So the floor goes in the database, where
no code path can be on the wrong side of it.

WHY ``NOT VALID`` AND NOT A PLAIN CHECK
--------------------------------------
There are 1,685 historical ``scores`` rows and 106 ``confirmation_scores`` rows
below v7. They are the subnet's audit trail and they must stay readable,
queryable and byte-identical -- this change blocks new writes, it is not a
purge. A validated CHECK would refuse to be created at all against that data.

``NOT VALID`` is precisely the tool: Postgres skips the existing-row scan but
enforces the predicate on every INSERT and on every UPDATE from the moment it
commits. Historical rows keep their values and stay fully selectable; new
retired-era writes are rejected by the storage engine. Skipping the scan is
also what keeps this migration metadata-only -- no ``ScanTable``, no rewrite,
one short ``ACCESS EXCLUSIVE`` window per table.

A deliberate consequence: UPDATEs to existing sub-v7 score rows are now
rejected too, since Postgres checks the new row version. That is intended.
``replace_validator_score`` and the score upsert are the two writers that
reach these rows, and neither should be able to rewrite a retired era's
ledger. Reads are entirely unaffected.

WHY ``validator_tickets`` GETS A TRIGGER INSTEAD
------------------------------------------------
A CHECK on ``validator_tickets`` would be wrong, and dangerously so. Sub-v7
leases drain within a <=90 minute TTL, and draining one is an UPDATE
(``issued`` -> ``expired``/``scored``, plus the overdue sweep's
``retry_after``). A CHECK -- even NOT VALID -- fails those UPDATEs, which would
strand every in-flight v6 lease in ``issued`` forever with no way to close it.
That is the opposite of a clean drain.

So the ticket floor is a trigger, which can see the TRANSITION rather than
just the resulting row. It refuses inserting a sub-v7 ticket and refuses
LEASING one -- whether that means reviving an expired row or renewing a live
one -- and permits everything else. New retired-era admission is impossible and
so is re-leasing, while an existing lease keeps every state change it needs to
reach a terminal state and disappear.

The lease arm is load-bearing, not belt-and-braces. Leasing a ticket is an
UPDATE, so an INSERT-only guard would have left
``replace_validator_score_after_infrastructure_failure`` (no era check at all)
and the score-retest queue (guarded only by advertised validator capability)
able to re-open a v6 lease from Backroom. And watching only the status column
would still have missed the case that actually happened in production:
``issue_ticket``'s reuse branch re-leases a row that is ALREADY ``issued`` by
pushing its deadline out, which is how the source-backfill resume path kept a
v6 lease alive indefinitely. The guard keys on the lease moving, not on the
status changing.

(As of this writing there are zero ``issued`` sub-v7 tickets -- the newest
sub-v7 deadline passed on 2026-07-26 -- so the drain set is empty in practice.
The trigger is shaped for correctness, not for a population that happens to be
zero today.)

``validator_tickets`` is a hot table (#481/#483). Creating a trigger takes a
brief ``ACCESS EXCLUSIVE`` lock and nothing else: no scan, no rewrite. It is
the only hot table this migration touches, and it is touched in its own
statement. ``alembic/env.py``'s ``lock_timeout`` and bounded retry are the
backstop underneath, as with ``validator_tickets_purpose_guard``, which is the
same shape.

REVERSIBILITY
-------------
Fully reversible. ``downgrade()`` drops four constraints and one trigger plus
its function; it restores nothing, because nothing was dropped, rewritten or
backfilled on the way up. No data is read or modified in either direction.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4b8e6c1a205"
down_revision: str | Sequence[str] | None = "45ef71514f21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Keep in step with ``ditto.db.queries.benchmark_rollout``'s
# MIN_SCOREABLE_BENCH_VERSION. Inlined rather than imported: a migration must
# keep describing the schema it created even after the constant moves on.
_FLOOR = 7


def upgrade() -> None:
    # Score ledgers. NOT VALID -- enforced for new rows, silent about old ones.
    op.execute(
        f"ALTER TABLE scores ADD CONSTRAINT scores_bench_version_floor "
        f"CHECK (bench_version >= {_FLOOR}) NOT VALID"
    )
    op.execute(
        "ALTER TABLE confirmation_scores "
        "ADD CONSTRAINT confirmation_scores_bench_version_floor "
        f"CHECK (bench_version >= {_FLOOR}) NOT VALID"
    )
    # No future rollout may target a retired era. With the existing
    # ``benchmark_rollout_forward`` (desired > from) this pins from_version >= 6
    # on any new row, so the earliest transition still openable ends at v7.
    op.execute(
        "ALTER TABLE benchmark_rollouts "
        "ADD CONSTRAINT benchmark_rollout_desired_floor "
        f"CHECK (desired_version >= {_FLOOR}) NOT VALID"
    )
    # Admission floor: no new retired-era lease, by INSERT or by RE-ISSUE.
    #
    # A plain BEFORE INSERT trigger is not enough, and the gap is not
    # theoretical. Re-leasing a ticket is an UPDATE, not an INSERT -- the row
    # already exists and its status flips back to ``issued`` -- so an
    # INSERT-only guard leaves every reissue path open. Two of them can be
    # driven straight from Backroom against a v6 agent:
    # ``replace_validator_score_after_infrastructure_failure`` (which had no
    # era check of any kind) and the score-retest queue (guarded only by what
    # the validator advertises it can run, which is a capability check, not a
    # policy floor).
    #
    # The UPDATE arm has to catch RENEWAL, not just resurrection. "Was not
    # issued, is now issued" is the obvious half and it is not sufficient:
    # ``issue_ticket``'s reuse branch writes ``status = ISSUED`` over a row
    # that is ALREADY issued and simply pushes ``deadline`` out. That is
    # precisely what the source-backfill resume path did to keep a v6 lease
    # alive indefinitely, so a guard that only watched the status column would
    # have reproduced the original hole in the schema.
    #
    # A sub-v7 row is therefore refused whenever it ends up ``issued`` AND
    # anything about the lease itself moved: the status came from somewhere
    # else, the ``issued_at`` stamp was refreshed, or the deadline was pushed
    # forward. Together those cover every way ``issue_ticket`` hands out a
    # lease, whatever state the row was in when it started.
    #
    # Everything an in-flight lease needs in order to DRAIN stays writable:
    # ``issued -> expired`` and ``issued -> scored`` (the row stops being
    # issued), the overdue sweep's ``retry_after``, ``force_expire_lease``
    # (which moves the deadline backwards, not forwards), and
    # ``failure_reason`` / ``first_reported_at`` touches on a still-live lease.
    # A CHECK constraint could not draw this line at all -- it sees only the
    # new row, never the transition -- which is why this is a trigger.
    op.execute(
        f"""
        CREATE FUNCTION guard_validator_ticket_bench_floor() RETURNS trigger AS $$
        BEGIN
            IF NEW.bench_version < {_FLOOR}
               AND (TG_OP = 'INSERT'
                    OR (NEW.status = 'issued'
                        AND (OLD.status <> 'issued'
                             OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
                             OR NEW.deadline > OLD.deadline))) THEN
                RAISE EXCEPTION
                    'benchmark v% is retired and cannot be leased',
                    NEW.bench_version
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'validator_tickets_bench_version_floor';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER validator_tickets_bench_version_floor
        BEFORE INSERT OR UPDATE ON validator_tickets
        FOR EACH ROW
        EXECUTE FUNCTION guard_validator_ticket_bench_floor()
        """
    )
    # Both tables still carried a server-side ``DEFAULT 2`` from the migrations
    # that introduced the column, when 2 was the only benchmark there was. Under
    # the floor that default is not merely stale, it is unreachable: any INSERT
    # that omitted the column would take it and then immediately fail the CHECK
    # (or the trigger). A default whose only possible effect is an error is
    # worse than no default, because it reads as though omitting the column is
    # supported. Drop both -- the column stays NOT NULL, so omitting it is now
    # an honest, immediate "null value in column" instead.
    op.execute("ALTER TABLE scores ALTER COLUMN bench_version DROP DEFAULT")
    op.execute("ALTER TABLE validator_tickets ALTER COLUMN bench_version DROP DEFAULT")


def downgrade() -> None:
    # Restore the historical server-side defaults exactly as they were before
    # this migration, so a downgrade leaves the schema byte-for-byte reversible
    # rather than only functionally so.
    op.execute("ALTER TABLE scores ALTER COLUMN bench_version SET DEFAULT 2")
    op.execute("ALTER TABLE validator_tickets ALTER COLUMN bench_version SET DEFAULT 2")
    op.execute(
        "DROP TRIGGER IF EXISTS validator_tickets_bench_version_floor "
        "ON validator_tickets"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_validator_ticket_bench_floor()")
    op.execute(
        "ALTER TABLE benchmark_rollouts "
        "DROP CONSTRAINT IF EXISTS benchmark_rollout_desired_floor"
    )
    op.execute(
        "ALTER TABLE confirmation_scores "
        "DROP CONSTRAINT IF EXISTS confirmation_scores_bench_version_floor"
    )
    op.execute(
        "ALTER TABLE scores DROP CONSTRAINT IF EXISTS scores_bench_version_floor"
    )
