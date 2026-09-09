"""Exercise the installed adapter and Pylon task lifecycle without network/signing."""

from __future__ import annotations

import ast
import importlib.metadata
import inspect
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bittensor_drand
from ditto_pylon_epoch import SCHEDULE_VERSION, EpochSchedule
from pylon_service.api._unstable import services, tasks
from pylon_service.bittensor import contact as contact_module
from pylon_service.bittensor.contact import TurboBtContact
from pylon_service.bittensor.contact_router import BittensorContactRouter
from turbobt.subnet import SubnetWeights


def schedule(block=9_029_448, **changes):
    return EpochSchedule(
        **{
            "last_epoch_block": 9_029_149,
            "pending_epoch_at": 0,
            "subnet_epoch_index": 25_016,
            "tempo": 360,
            "blocks_since_last_step": block - 9_029_149,
            "current_block": block,
            **changes,
        }
    )


class BlockContext:
    number = 9_029_448
    hash = "0x" + "a" * 64

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def test_installed_dependencies_and_entrypoints(self):
        self.assertEqual(importlib.metadata.version("bittensor-drand"), "2.0.0")
        self.assertEqual(importlib.metadata.version("turbobt"), "1.3.1")
        self.assertIn(TypeError, contact_module.RECONNECT_EXCEPTIONS)
        self.assertEqual(SCHEDULE_VERSION, "subtensor-stateful-epochs-v1")
        self.assertTrue(callable(TurboBtContact.get_epoch_schedule))
        self.assertTrue(callable(BittensorContactRouter.get_epoch_schedule))
        source = inspect.getsource(SubnetWeights.commit)
        self.assertIn("get_encrypted_commit_v2", source)
        self.assertNotIn("get_encrypted_commit(", source)
        self.assertNotIn(
            "get_epoch_containing_block", inspect.getsource(tasks.ApplyWeights)
        )
        self.assertNotIn(
            "get_epoch_containing_block", inspect.getsource(services.WeightService)
        )

    def test_every_installed_drand_call_exists_in_v2(self):
        calls = set()
        roots = (
            Path("/app/pylon_service/pylon_service"),
            Path("/app/pylon_service/.venv/lib/python3.13/site-packages/turbobt"),
        )
        for root in roots:
            for path in root.rglob("*.py"):
                source = path.read_text()
                if "bittensor_drand." not in source:
                    continue
                for node in ast.walk(ast.parse(source)):
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "bittensor_drand"
                    ):
                        calls.add(node.attr)
        self.assertEqual(calls, {"get_encrypted_commit_v2", "get_encrypted_commitment"})
        self.assertTrue(all(hasattr(bittensor_drand, name) for name in calls))

    async def test_weight_status_uses_the_same_stateful_window_as_tasks(self):
        block = SimpleNamespace(number=9_029_448, hash=BlockContext.hash)
        contact = SimpleNamespace(
            get_block=AsyncMock(return_value=block),
            get_epoch_schedule=AsyncMock(return_value=schedule()),
        )
        identity = SimpleNamespace(identity_name="synthetic")
        service = services.WeightService(identity, contact)
        with patch.object(
            services, "weight_task_submitted", AsyncMock(return_value=True)
        ) as read:
            result = await service.get_weight_status(118, 0, block.number)
        self.assertTrue(result.weights_submitted)
        args = read.await_args.args
        self.assertEqual(args[:2], (identity, 0))
        self.assertEqual((args[2].start, args[2].end), (9_029_149, 9_029_508))

    async def test_weight_status_includes_a_deferred_boundary_block(self):
        block = SimpleNamespace(number=9_029_509, hash=BlockContext.hash)
        deferred = schedule(
            block=block.number,
            pending_epoch_at=9_029_510,
            blocks_since_last_step=360,
        )
        contact = SimpleNamespace(
            get_block=AsyncMock(return_value=block),
            get_epoch_schedule=AsyncMock(return_value=deferred),
        )
        identity = SimpleNamespace(identity_name="synthetic")
        service = services.WeightService(identity, contact)
        with patch.object(
            services, "weight_task_submitted", AsyncMock(return_value=True)
        ) as read:
            await service.get_weight_status(118, 0, block.number)
        interval = read.await_args.args[2]
        self.assertEqual((interval.start, interval.end), (9_029_149, 9_029_509))

    def test_real_drand_v2_uses_stateful_reveal_target(self):
        # Encryption only: a public synthetic hotkey, no secret key and no RPC.
        # Keep upstream's inclusion delay and +3 security blocks intact.
        before = time.time()
        body, reveal = bittensor_drand.get_encrypted_commit_v2(
            uids=[43],
            weights=[65535],
            version_key=1,
            **schedule().drand_arguments(),
            subnet_reveal_period_epochs=1,
            block_time=12.0,
            hotkey=bytes(32),
        )
        after = time.time()
        self.assertTrue(body)
        delay = (9_029_509 + 3 - 9_029_448) * 12
        self.assertGreaterEqual(reveal, int((before + delay - 1_692_803_367) // 3))
        self.assertLessEqual(reveal, int((after + delay - 1_692_803_367) // 3))

    def test_next_block_inclusion_does_not_assign_a_boundary_commit_to_old_epoch(self):
        before = time.time()
        _, reveal = bittensor_drand.get_encrypted_commit_v2(
            uids=[43],
            weights=[65535],
            version_key=1,
            **schedule(block=9_029_508).drand_arguments(),
            subnet_reveal_period_epochs=1,
            block_time=12.0,
            hotkey=bytes(32),
        )
        after = time.time()
        # Inclusion at 9029509 belongs to epoch 25017, not 25016.
        delay = (9_029_869 + 3 - 9_029_508) * 12
        self.assertGreaterEqual(reveal, int((before + delay - 1_692_803_367) // 3))
        self.assertLessEqual(reveal, int((after + delay - 1_692_803_367) // 3))

    async def test_turbobt_reads_one_block_and_preserves_submission(self):
        pinned = schedule()
        values = pinned.drand_arguments()
        names = {
            "LastEpochBlock": "last_epoch_block",
            "PendingEpochAt": "pending_epoch_at",
            "SubnetEpochIndex": "subnet_epoch_index",
            "Tempo": "tempo",
            "BlocksSinceLastStep": "blocks_since_last_step",
        }

        async def read(name, netuid, *, block_hash):
            self.assertEqual((netuid, block_hash), (118, BlockContext.hash))
            return values[names[name.split(".")[1]]]

        finalization = AsyncMock()
        submit = AsyncMock(
            return_value=SimpleNamespace(wait_for_finalization=finalization)
        )
        client = SimpleNamespace(
            blocks={-1: BlockContext()},
            wallet=SimpleNamespace(hotkey=SimpleNamespace(public_key=bytes(32))),
            subtensor=SimpleNamespace(
                state=SimpleNamespace(getStorage=AsyncMock(side_effect=read)),
                subtensor_module=SimpleNamespace(
                    commit_timelocked_mechanism_weights=submit
                ),
            ),
        )
        subnet = SimpleNamespace(
            netuid=118,
            client=client,
            get_hyperparameters=AsyncMock(
                return_value={"tempo": 360, "commit_reveal_period": 1}
            ),
        )
        weights = SubnetWeights(subnet)
        with patch(
            "bittensor_drand.get_encrypted_commit_v2", return_value=(b"ciphertext", 123)
        ) as encrypt:
            self.assertEqual(
                await weights.commit({43: 0.65, 104: 0.14}, mechanism_id=1), 123
            )
        arguments = encrypt.call_args.kwargs
        self.assertEqual({k: arguments[k] for k in values}, values)
        self.assertEqual(encrypt.call_args.args[0], (43, 104))
        self.assertEqual(encrypt.call_args.args[1][0], 65535)
        submit.assert_awaited_once_with(
            118, b"ciphertext", 1, 123, commit_reveal_version=4, wallet=client.wallet
        )
        finalization.assert_awaited_once()

    async def test_persisted_task_uses_epoch_counter_and_refuses_cross_epoch_retry(
        self,
    ):
        block = SimpleNamespace(number=9_029_448, hash=BlockContext.hash)
        contact = SimpleNamespace(
            hotkey="synthetic",
            get_block=AsyncMock(return_value=block),
            get_latest_block=AsyncMock(return_value=block),
            get_epoch_schedule=AsyncMock(return_value=schedule()),
        )
        task = tasks.ApplyWeights(
            SimpleNamespace(identity_name="synthetic"), contact, {}, 118, 0
        )
        task._task_id = 1
        task._start_block_number = block.number  # persisted request, including restart
        await task._prepare()
        self.assertEqual(task._initial_tempo.end, 9_029_508)
        self.assertEqual(task._initial_epoch_index, 25_016)
        task._apply_weights = AsyncMock()
        with (
            patch.object(
                tasks,
                "get_weight_task_status",
                AsyncMock(return_value=tasks.TaskStatus.RUNNING),
            ),
            patch.object(tasks, "update_weight_task_status", AsyncMock()) as update,
        ):
            await task._single_attempt()
            task._apply_weights.assert_awaited_once_with(block)
            # A manual early epoch also expires a task, even before old end.
            contact.get_epoch_schedule.return_value = schedule(
                last_epoch_block=block.number, subnet_epoch_index=25_017
            )
            with self.assertRaises(tasks.StopRetrying):
                await task._single_attempt()
            self.assertEqual(task._apply_weights.await_count, 1)
            update.assert_awaited_with(1, tasks.TaskStatus.EXPIRED)

    async def test_missing_schedule_never_encrypts_or_submits(self):
        contact = SimpleNamespace(
            hotkey="synthetic",
            get_latest_block=AsyncMock(return_value=SimpleNamespace(number=100)),
            get_epoch_schedule=AsyncMock(side_effect=RuntimeError("missing schedule")),
        )
        task = tasks.ApplyWeights(
            SimpleNamespace(identity_name="synthetic"), contact, {}, 118, 0
        )
        task._task_id = 1
        with (
            patch.object(tasks, "set_weight_task_start_block_number", AsyncMock()),
            self.assertRaises(RuntimeError),
        ):
            await task._prepare()
        self.assertIsNone(task._initial_tempo)

    async def test_deferred_boundary_does_not_expire_the_current_epoch_task(self):
        block = SimpleNamespace(number=9_029_509, hash=BlockContext.hash)
        deferred = schedule(
            block=block.number,
            pending_epoch_at=9_029_510,
            blocks_since_last_step=360,
        )
        contact = SimpleNamespace(
            hotkey="synthetic",
            get_block=AsyncMock(return_value=block),
            get_latest_block=AsyncMock(return_value=block),
            get_epoch_schedule=AsyncMock(return_value=deferred),
        )
        task = tasks.ApplyWeights(
            SimpleNamespace(identity_name="synthetic"), contact, {}, 118, 0
        )
        task._task_id = 1
        task._start_block_number = block.number
        await task._prepare()
        task._apply_weights = AsyncMock()
        with (
            patch.object(
                tasks,
                "get_weight_task_status",
                AsyncMock(return_value=tasks.TaskStatus.RUNNING),
            ),
            patch.object(tasks, "update_weight_task_status", AsyncMock()) as update,
        ):
            await task._single_attempt()
        task._apply_weights.assert_awaited_once_with(block)
        update.assert_awaited_once_with(
            1, tasks.TaskStatus.SUCCEEDED, only_if_running=True
        )


if __name__ == "__main__":
    unittest.main()
