"""Exercise installed receipt adapter against isolated SQLite, without wallets."""

from __future__ import annotations

import copy
import hashlib
import inspect
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import ditto_pylon_receipts as receipt
from litestar.exceptions import HTTPException
from pylon_service.api._unstable import tasks
from pylon_service.api._unstable.api import IdentityController
from pylon_service.db.database import Base
from pylon_service.db.models import WeightTask
from pylon_service.guards import identity_auth_guard
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from turbobt.subnet import SubnetWeights
from turbobt.substrate.extrinsic import ExtrinsicResult


def body():
    weights = {"miner": 0.9, "burn": 0.1}
    return {
        "schema_version": 1,
        "mechanism_id": 0,
        "weights": weights,
        "provenance": {
            "ledger_snapshot_id": str(uuid4()),
            "champion_agent_id": str(uuid4()),
            "champion_artifact_sha256": "a" * 64,
            "ledger_digest": "b" * 64,
            "vector_digest": receipt.canonical_digest(weights),
            "epoch_index": 12,
            "bench_version": 13,
        },
    }


class ReceiptImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///" + str(Path(self.tmp.name) / "receipts.sqlite")
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db_patch = patch.object(receipt, "session_factory", self.factory)
        self.db_patch.start()
        self.runner = MagicMock()
        self.active_patch = patch.object(tasks.ApplyWeights, "tasks_running", set())
        self.active_patch.start()
        self.runner.schedule = AsyncMock(
            side_effect=lambda: tasks.ApplyWeights.tasks_running.add(self.runner)
        )

        def construct(_identity, _contact, task):
            self.runner._task_id = task.id
            return self.runner

        self.run_patch = patch.object(
            tasks.ApplyWeights, "from_persisted_task", side_effect=construct
        )
        self.run_patch.start()
        self.service = SimpleNamespace(
            identity=SimpleNamespace(identity_name="validator"),
            contact_router=SimpleNamespace(hotkey="validator-hotkey"),
        )

    async def asyncTearDown(self):
        self.run_patch.stop()
        self.active_patch.stop()
        self.db_patch.stop()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def create(self, data=None):
        return await receipt.create_request(
            self.service, 118, str(uuid4()), data or body()
        )

    async def prepare(self, _row):
        await receipt.prepare_commit(
            118, 0, {1: 65535, 0: 7282}, b"encrypted-weight-payload", 1234, 100
        )

    def events(self):
        return [
            {"module_id": "System", "event_id": "ExtrinsicSuccess"},
            {
                "module_id": "SubtensorModule",
                "event_id": "TimelockedWeightsCommitted",
                "event": {
                    "attributes": [
                        "validator-hotkey",
                        118,
                        "0x"
                        + hashlib.blake2b(
                            b"encrypted-weight-payload", digest_size=32
                        ).hexdigest(),
                        1234,
                    ]
                },
            },
        ]

    async def finish(self):
        await receipt.finalize_commit(
            "0x" + "c" * 64,
            {"block": {"header": {"number": "0x400"}}},
            "0x" + "d" * 64,
            2,
            self.events(),
        )

    async def test_idempotent_and_conflicting_submission(self):
        request_id, data = str(uuid4()), body()
        first = await receipt.create_request(self.service, 118, request_id, data)
        again = await receipt.create_request(self.service, 118, request_id, data)
        self.assertEqual(first["task_id"], again["task_id"])
        self.runner.schedule.assert_awaited_once()
        changed = copy.deepcopy(data)
        changed["provenance"]["champion_agent_id"] = str(uuid4())
        with self.assertRaises(HTTPException) as caught:
            await receipt.create_request(self.service, 118, request_id, changed)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_dispatch_failure_is_reconciled_without_new_task(self):
        request_id, data = str(uuid4()), body()
        self.runner.schedule.side_effect = RuntimeError("simulated dispatch crash")
        with self.assertRaises(RuntimeError):
            await receipt.create_request(self.service, 118, request_id, data)
        before = await receipt.get_request(self.service, 118, request_id)
        self.runner.schedule.side_effect = lambda: tasks.ApplyWeights.tasks_running.add(
            self.runner
        )
        after = await receipt.create_request(self.service, 118, request_id, data)
        self.assertEqual(before["task_id"], after["task_id"])
        self.assertEqual(self.runner.schedule.await_count, 2)

    async def test_new_subnet_request_does_not_cancel_other_subnet(self):
        first = await self.create()
        await receipt.create_request(self.service, 119, str(uuid4()), body())
        async with self.factory() as session:
            task = await session.get(WeightTask, first["task_id"])
            self.assertEqual(task.status, tasks.TaskStatus.RUNNING)

    async def test_same_vector_different_artifact_remains_separate(self):
        a = body()
        b = copy.deepcopy(a)
        b["provenance"]["champion_agent_id"] = str(uuid4())
        b["provenance"]["champion_artifact_sha256"] = "e" * 64
        first, second = await self.create(a), await self.create(b)
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertNotEqual(first["request_digest"], second["request_digest"])

    async def test_prepared_restart_never_resubmits(self):
        row = await self.create()
        async with receipt.record_task(row["task_id"]) as submit:
            self.assertTrue(submit)
            await self.prepare(row)
        # Simulates a new process: no ContextVar state, only durable DB state.
        with self.assertRaises(tasks.StopRetrying):
            async with receipt.record_task(row["task_id"]):
                self.fail("uncertain transmission retried")
        recovery = await receipt.get_request(self.service, 118, row["request_id"])
        self.assertEqual(recovery["status"], "uncertain")
        replacement = await self.create()
        async with receipt.record_task(replacement["task_id"]) as submit:
            self.assertTrue(submit)

    async def test_finalized_restart_list_ack_and_tombstone(self):
        row = await self.create()
        async with receipt.record_task(row["task_id"]):
            await self.prepare(row)
            await self.finish()
        async with receipt.record_task(row["task_id"]) as submit:
            self.assertFalse(submit)
        page = await receipt.list_requests(self.service, 118, 0, 1)
        final = page["receipts"][0]
        self.assertEqual(final["attempts"][0]["commit_block"], 1024)
        await receipt.acknowledge_request(
            self.service,
            118,
            row["request_id"],
            {
                "request_digest": row["request_digest"],
                "attempt_id": final["attempts"][0]["attempt_id"],
                "receipt_digest": receipt.canonical_digest(
                    receipt.immutable_receipt(final)
                ),
            },
        )
        self.assertEqual(
            (await receipt.list_requests(self.service, 118, 0, 1))["receipts"], []
        )
        async with self.factory() as session, session.begin():
            await session.execute(
                update(receipt.receipts)
                .where(receipt.receipts.c.request_id == row["request_id"])
                .values(acknowledged_at=datetime.now(UTC) - timedelta(days=91))
            )
        await self.create()
        retained = await receipt.create_request(
            self.service,
            118,
            row["request_id"],
            {
                key: row[key]
                for key in ("schema_version", "mechanism_id", "weights", "provenance")
            },
        )
        self.assertEqual(retained["task_id"], row["task_id"])
        self.assertEqual(retained["status"], "retained")
        self.assertTrue(retained["acknowledged"])

    async def test_real_alembic_upgrade_preserves_tasks_and_rollback_evidence(self):
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from pylon_service.settings import database_settings
        from sqlalchemy import create_engine, text

        database = Path(self.tmp.name) / "upgrade.sqlite"
        cfg = Config("/app/pylon_service/alembic.ini")
        cfg.set_main_option(
            "script_location", "/app/pylon_service/pylon_service/db/migrations"
        )
        self.assertEqual(
            ScriptDirectory.from_config(cfg).get_heads(), ["ditto_receipts_v1"]
        )
        engine = create_engine("sqlite:///" + str(database))
        with patch.object(
            type(database_settings),
            "get_url",
            return_value="sqlite:///" + str(database),
        ):
            command.upgrade(cfg, "daab35a40458")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO weight_tasks "
                        "(id,status,identity_name,weights,netuid,mechanism_id,hotkey) "
                        "VALUES (1,'RUNNING','validator','{}',118,0,'validator-hotkey')"
                    )
                )
            command.upgrade(cfg, "head")
            command.upgrade(cfg, "head")
            with engine.begin() as connection:
                self.assertEqual(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM weight_tasks "
                            "WHERE id=1 AND status='RUNNING'"
                        )
                    ),
                    1,
                )
                connection.execute(
                    text(
                        "INSERT INTO ditto_weight_receipts "
                        "(request_id,task_id,identity_name,netuid,request_digest,"
                        "body,acknowledged,created_at) VALUES "
                        "('retained',1,'validator',118,'digest','{}',0,CURRENT_TIMESTAMP)"
                    )
                )
            command.downgrade(cfg, "daab35a40458")
            with engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(
                        text("SELECT count(*) FROM ditto_weight_receipts")
                    ),
                    1,
                )
                self.assertEqual(
                    connection.scalar(text("SELECT version_num FROM alembic_version")),
                    "daab35a40458",
                )
            command.upgrade(cfg, "head")
            with engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(
                        text("SELECT count(*) FROM ditto_weight_receipts")
                    ),
                    1,
                )
                self.assertEqual(
                    connection.scalar(text("SELECT count(*) FROM weight_tasks")), 1
                )
        engine.dispose()

    async def test_installed_envelope_matches_shared_receipt_digest(self):
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "weight_receipt_contract", "/tmp/weight_receipt_contract.py"
        )
        contract = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = contract
        spec.loader.exec_module(contract)
        row = await self.create()
        async with receipt.record_task(row["task_id"]):
            await self.prepare(row)
            await self.finish()
        final = await receipt.get_request(self.service, 118, row["request_id"])
        raw = receipt.immutable_receipt(final)
        claim = contract.FinalizedWeightReceipt.model_validate(raw)
        self.assertEqual(
            receipt.canonical_digest(raw), contract.weight_receipt_digest(claim)
        )

    async def test_identity_and_ack_are_scoped(self):
        row = await self.create()
        other = SimpleNamespace(identity=SimpleNamespace(identity_name="other"))
        with self.assertRaises(HTTPException) as caught:
            await receipt.get_request(other, 118, row["request_id"])
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            await receipt.acknowledge_request(self.service, 118, row["request_id"], {})
        self.assertEqual(
            (await receipt.list_requests(other, 118, 0, 10))["receipts"], []
        )
        self.assertIn(identity_auth_guard, IdentityController.guards)

    async def test_finalization_wrong_commit_hash_fails_closed(self):
        row = await self.create()
        async with receipt.record_task(row["task_id"]):
            await self.prepare(row)
            events = self.events()
            events[1]["event"]["attributes"][2] = "0x" + "f" * 64
            with self.assertRaises(ValueError):
                await receipt.finalize_commit(
                    "0x" + "c" * 64,
                    {"block": {"header": {"number": 1024}}},
                    "0x" + "d" * 64,
                    2,
                    events,
                )
        self.assertEqual(
            (await receipt.get_request(self.service, 118, row["request_id"]))["status"],
            "uncertain",
        )

    async def test_actual_finalization_adapter_captures_receipt(self):
        row = await self.create()
        extrinsic_hash = "0x" + "d" * 64

        class Subscription:
            id = "mock-subscription"

            def __aiter__(self):
                async def statuses():
                    yield {"finalized": "0x" + "c" * 64}

                return statuses()

        events = [{**event, "extrinsic_idx": 0} for event in self.events()]
        substrate = SimpleNamespace(
            author=SimpleNamespace(unwatchExtrinsic=AsyncMock()),
            chain=SimpleNamespace(
                getBlock=AsyncMock(
                    return_value={
                        "block": {
                            "header": {"number": "0x400"},
                            "extrinsics": [{"extrinsic_hash": extrinsic_hash}],
                        }
                    }
                )
            ),
            system=SimpleNamespace(
                Events=SimpleNamespace(get=AsyncMock(return_value=events))
            ),
        )
        tx = ExtrinsicResult(
            SimpleNamespace(extrinsic_hash=bytes.fromhex("d" * 64)),
            Subscription(),
            substrate,
        )
        async with receipt.record_task(row["task_id"]):
            await self.prepare(row)
            await tx.wait_for_finalization()
        final = await receipt.get_request(self.service, 118, row["request_id"])
        self.assertEqual(final["status"], "finalized")
        self.assertEqual(final["attempts"][0]["extrinsic_index"], 0)

    async def test_receipt_routes_reuse_identity_guard(self):
        import ditto_pylon_receipt_api as api
        from litestar import Controller, Litestar
        from litestar.di import Provide
        from litestar.testing import TestClient
        from pylon_service import guards

        class ReceiptController(Controller):
            path = "/identity/{identity_name:str}/subnet/{netuid:int}"
            guards = [identity_auth_guard]
            dependencies = {
                "unstable_weight_service": Provide(
                    lambda: self.service, sync_to_thread=False
                )
            }
            get_weight_receipt = api.get_weight_receipt

        request_id = str(uuid4())
        route = f"/identity/validator/subnet/118/ditto/weight-receipts/{request_id}"
        with (
            patch.dict(
                guards.identities,
                {"validator": SimpleNamespace(token="test-token", netuid=118)},
            ),
            patch.object(
                api, "get_request", AsyncMock(return_value={"request_id": request_id})
            ),
            TestClient(Litestar(route_handlers=[ReceiptController])) as client,
        ):
            self.assertEqual(client.get(route).status_code, 401)
            self.assertEqual(
                client.get(
                    route, headers={"Authorization": "Bearer wrong"}
                ).status_code,
                403,
            )
            response = client.get(route, headers={"Authorization": "Bearer test-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["request_id"], request_id)

    async def test_request_boundaries_and_wrong_ack_digest(self):
        for mutate in (
            lambda b: b.update(schema_version=True),
            lambda b: b["weights"].update(miner=2.0),
            lambda b: b["provenance"].update(bench_version=0),
        ):
            invalid = body()
            mutate(invalid)
            with self.assertRaises(ValueError):
                receipt.validate_request(invalid)
        row = await self.create()
        async with receipt.record_task(row["task_id"]):
            await self.prepare(row)
            await self.finish()
        final = await receipt.get_request(self.service, 118, row["request_id"])
        with self.assertRaises(HTTPException):
            await receipt.acknowledge_request(
                self.service,
                118,
                row["request_id"],
                {
                    "request_digest": row["request_digest"],
                    "attempt_id": final["attempts"][0]["attempt_id"],
                    "receipt_digest": "f" * 64,
                },
            )
        self.assertFalse(
            (await receipt.get_request(self.service, 118, row["request_id"]))[
                "acknowledged"
            ]
        )

    async def test_installed_capture_order_and_legacy_noop(self):
        source = inspect.getsource(SubnetWeights.commit)
        self.assertLess(
            source.index("await prepare_commit"),
            source.index("commit_timelocked_mechanism_weights"),
        )
        await receipt.prepare_commit(118, 0, {}, b"", 1, 1)
        await receipt.finalize_commit("", {}, "", 0, [])
        async with self.factory() as session:
            self.assertEqual((await session.execute(select(WeightTask))).all(), [])


if __name__ == "__main__":
    unittest.main()
