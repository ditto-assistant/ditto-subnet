"""Default-off durable outbox/inbox. No private key or transaction submission."""

import asyncio
import logging
import os
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select

from ditto.api_server.treasury_ingress import ReceiptHistoryUnavailable
from ditto.api_server.treasury_manual import (
    ACTIVE,
    InvalidManualReport,
    accept_report,
    lock_manual,
    publish_audit,
)
from ditto.api_server.treasury_runtime import lock_runtime, treasury_runtime
from ditto.chain.errors import ChainError
from ditto.db.models import TreasuryManualTransfer as Transfer
from ditto_screening_protocol.treasury_pubsub import TreasuryMailbox

logger = logging.getLogger(__name__)


class TreasuryManualLoop:
    def __init__(self, app_state, *, mailbox=None):
        self.state = app_state
        self.mailbox = mailbox
        mode = os.environ.get("DITTO_TREASURY_MANUAL_ENABLED", "false")
        if mode not in {"true", "false"}:
            raise ValueError("Manual bridge requires explicit true or false")
        if mailbox is None and mode == "true":
            self.mailbox = TreasuryMailbox(
                project=os.environ["DITTO_TREASURY_MANUAL_PROJECT"],
                topic=os.environ["DITTO_TREASURY_MANUAL_REQUEST_TOPIC"],
                subscription=os.environ["DITTO_TREASURY_MANUAL_REPORT_SUBSCRIPTION"],
            )
        self.task = None
        self.last_error = None

    @property
    def enabled(self):
        return self.mailbox is not None

    async def start(self):
        if self.enabled:
            self.task = asyncio.create_task(self.run(), name="treasury-manual")

    async def aclose(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def sweep(self):
        message = await asyncio.to_thread(self.mailbox.pull)
        if message:
            ack, body = message
            try:
                async with self.state.session_maker() as session, session.begin():
                    await accept_report(session, self.state.config, body)
            except InvalidManualReport:
                # Permanent contract/provenance rejection rolls back the inbox.
                # Drop only these bytes, never a valid report on an outage.
                logger.warning("manual custody report permanently refused")
            # ACK after durable inbox commit, never before independent audit.
            # The audit remains retryable in SQL even after this ACK is lost.
            await asyncio.to_thread(self.mailbox.ack, ack)
        async with self.state.session_maker() as session, session.begin():
            await lock_runtime(session)
            await lock_manual(session)
            row = await session.scalar(
                select(Transfer)
                .where(Transfer.status.in_(ACTIVE))
                .order_by(Transfer.created_at)
                .limit(1)
            )
            if row is None:
                return
            if row.status == "queued":
                runtime = await treasury_runtime(session, self.state.config)
                if not runtime.treasury_weight_enforcement or (
                    runtime.treasury_approved_collector_policy_digest
                    != row.envelope["collector_policy_digest"]
                ):
                    row.status, row.last_error = (
                        "refused",
                        "Gamma paused or signed policy changed; dispatch stopped, "
                        "prior delivery may still settle",
                    )
                else:
                    # Publish-before-commit is safe only because custody binds
                    # exact UUID/body and signed bytes before sending anything.
                    await asyncio.to_thread(self.mailbox.publish, row.envelope)
                    row.status = "dispatched"
                row.updated_at = datetime.now(UTC)
        async with self.state.session_maker() as session, session.begin():
            row = await session.scalar(
                select(Transfer)
                .where(Transfer.status == "audit_pending")
                .order_by(Transfer.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if (
                row
                and row.last_error
                != "Finalized receipt proof refused; operator review required"
            ):
                try:
                    # A failed chain read rolls back only this attempt. Nothing
                    # invokes custody or resends funds to repair publication.
                    async with session.begin_nested():
                        await publish_audit(session, self.state.chain, row)
                except (ChainError, ReceiptHistoryUnavailable):
                    row.last_error = (
                        "Finalized receipt history temporarily unavailable; "
                        "automatic audit retry pending"
                    )
                except ValueError:
                    row.last_error = (
                        "Finalized receipt proof refused; operator review required"
                    )

    async def run(self):
        while True:
            try:
                await self.sweep()
                self.last_error = None
            except Exception as error:
                self.last_error = (
                    "Manual mailbox unavailable or invalid; "
                    "no new request is confirmed delivered"
                )
                logger.warning("manual mailbox blocked: %s", type(error).__name__)
            await asyncio.sleep(15)
