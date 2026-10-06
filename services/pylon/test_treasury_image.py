"""Actual installed queued guards; synthetic public approval, no network or funds."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from scalecodec.utils.ss58 import ss58_decode

import ditto_pylon_receipts as receipts
import ditto_pylon_treasury as treasury
from bittensor_wallet import Keypair
from pylon_service.api._unstable import tasks
from pylon_service.bittensor.contact import AbstractBittensorContact
from turbobt.subnet import SubnetWeights

from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    approval_message,
)
from ditto_screening_protocol.treasury_enforcement import (
    EnforcingTreasuryPin,
    TreasuryFleetMember,
)


class TreasuryImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        coldkey = Keypair.create_from_uri("//Alice")
        self.other = Keypair.create_from_uri("//Dave").ss58_address
        policy = TreasuryEmissionPolicy(
            revision=1,
            genesis_hash="0x" + "11" * 32,
            collector_hotkey=Keypair.create_from_uri("//Bob").ss58_address,
            collector_coldkey=coldkey.ss58_address,
            collector_policy_digest="c" * 64,
            buckets=[
                {
                    "bucket_id": "gamma",
                    "allocation_bps": 1000,
                    "holding_coldkey": Keypair.create_from_uri(
                        "//Charlie"
                    ).ss58_address,
                }
            ],
        )
        approval = TreasuryPolicyApproval(
            policy=policy,
            signature="0x" + coldkey.sign(approval_message(policy)).hex(),
        )
        self.member = TreasuryFleetMember(
            validator_hotkey=Keypair.create_from_uri("//Eve").ss58_address,
            protocol_version=30,
            treasury_pin_version=2,
            treasury_dispatch_version=2,
            approved_policy_digest=policy.digest,
            collector_policy_digest="c" * 64,
        )
        self.pin = EnforcingTreasuryPin(
            epoch_index=9,
            first_block=100,
            pinned_block=101,
            pinned_block_hash="0x" + "ab" * 32,
            policy=policy,
            policy_digest=policy.digest,
            approval=approval,
            fleet=[self.member],
            identity={
                "genesis_hash": policy.genesis_hash,
                "finalized_block": 101,
                "finalized_block_hash": "0x" + "ab" * 32,
                "uid": 0,
                "hotkey": policy.collector_hotkey,
                "uid_hotkey": policy.collector_hotkey,
                "owner_coldkey": policy.collector_coldkey,
                "subnet_owner_coldkey": self.other,
            },
        )
        self.body = {
            "schema_version": 2,
            "treasury_pin": self.pin.model_dump(mode="json"),
            "weights": {policy.collector_hotkey: 0.1, self.other: 0.9},
        }
        self.head = "0x" + "ac" * 32
        self.values = {
            "SubnetEpochIndex": 9,
            "LastEpochBlock": 100,
            "Owner": policy.collector_coldkey,
            "SubnetOwner": self.other,
            "Uids": 0,
            "Keys": policy.collector_hotkey,
        }

        async def storage(name, *_params, block_hash):
            self.assertEqual(block_hash, self.head)
            return self.values[name.split(".")[-1]]

        self.storage = AsyncMock(side_effect=storage)
        self.client = SimpleNamespace(
            subtensor=SimpleNamespace(
                rpc=AsyncMock(return_value=self.head),
                chain=SimpleNamespace(
                    getHeader=AsyncMock(return_value={"number": 102}),
                    getBlockHash=AsyncMock(
                        side_effect=lambda n: (
                            policy.genesis_hash if n == 0 else self.head
                        )
                    ),
                ),
                state=SimpleNamespace(getStorage=self.storage),
            )
        )
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "synthetic-public-approval.json"
        path.write_text(approval.model_dump_json())
        self.config = patch.dict(
            os.environ,
            {
                "DITTO_TREASURY_SHADOW_APPROVAL_FILE": str(path),
                "DITTO_TREASURY_APPROVED_POLICY_DIGEST": policy.digest,
                "DITTO_TREASURY_COLLECTOR_POLICY_DIGEST": "c" * 64,
                "DITTO_TREASURY_WEIGHT_ENFORCEMENT": "true",
            },
        )
        self.config.start()

    async def asyncTearDown(self):
        self.config.stop()
        self.tmp.cleanup()

    async def test_armed_transport_blocks_actual_legacy_queued_task_and_commit(self):
        from unittest.mock import MagicMock

        from pylon_service.api._unstable.tasks import ApplyWeights, StopRetrying

        task = MagicMock()
        task._client = MagicMock()
        with self.assertRaises(StopRetrying):
            await ApplyWeights._apply_weights(task, MagicMock())
        task._client.get_hyperparameters.assert_not_called()
        self.storage.assert_not_awaited()
        with self.assertRaises(StopRetrying):
            await treasury.guard_normalized_commit(self.client, 118, {0: 1})

    async def test_fresh_finalized_guard_uses_one_hash_for_all_storage(self):
        block = await treasury.queued_block(
            self.client, self.body, 118, self.member.validator_hotkey
        )
        self.assertEqual((block.number, block.hash), (102, self.head))
        self.assertEqual(self.storage.await_count, 6)
        self.client.subtensor.rpc.assert_awaited_once_with(
            method="chain_getFinalizedHead", params=[]
        )

    async def test_turbobt_wire_shapes_reach_guard_without_skipping_identity(self):
        async def rpc(*, method, params):
            self.assertEqual(method, "chain_getFinalizedHead")
            if params != []:
                raise ValueError("Invalid params")
            return bytearray.fromhex(self.head[2:])

        self.client.subtensor.rpc.side_effect = rpc
        for name in ("Owner", "SubnetOwner", "Keys"):
            self.values[name] = "0x" + ss58_decode(self.values[name])
        block = await treasury.queued_block(
            self.client, self.body, 118, self.member.validator_hotkey
        )
        self.assertEqual((block.number, block.hash), (102, self.head))
        self.assertEqual(self.storage.await_count, 6)
        self.client.subtensor.chain.getHeader.assert_awaited_once_with(self.head)
        # Normalization cannot turn a changed valid AccountId32 into authority.
        self.values["Owner"] = "0x" + ss58_decode(self.other)
        with self.assertRaises(tasks.StopRetrying):
            await treasury.queued_block(
                self.client, self.body, 118, self.member.validator_hotkey
            )

    async def test_malformed_finalized_hash_refuses_before_storage(self):
        for head in (bytearray(31), bytes(33), "0x" + "aa" * 31, None, True):
            with self.subTest(head_type=type(head).__name__):
                self.client.subtensor.rpc.return_value = head
                with self.assertRaises(tasks.StopRetrying):
                    await treasury.queued_block(
                        self.client, self.body, 118, self.member.validator_hotkey
                    )
        self.storage.assert_not_awaited()

    async def test_hex_accounts_are_normalized_with_text_head(self):
        for name in ("Owner", "SubnetOwner", "Keys"):
            self.values[name] = "0x" + ss58_decode(self.values[name])
        observed = await treasury.require_queued_binding(
            self.client,
            self.body,
            netuid=118,
            validator_hotkey=self.member.validator_hotkey,
        )
        self.assertEqual(
            observed.identity.owner_coldkey, self.pin.policy.collector_coldkey
        )
        self.assertEqual(observed.identity.uid_hotkey, self.pin.policy.collector_hotkey)

    async def test_malformed_account_storage_refuses(self):
        for malformed in ("0x" + "aa" * 31, "0x" + "gg" * 32, None, True):
            with self.subTest(account_type=type(malformed).__name__):
                self.values["Owner"] = malformed
                with self.assertRaises(tasks.StopRetrying):
                    await treasury.queued_block(
                        self.client, self.body, 118, self.member.validator_hotkey
                    )

    async def test_non_account_storage_is_not_normalized(self):
        self.values["Uids"] = "0x" + "aa" * 32
        with self.assertRaises(tasks.StopRetrying):
            await treasury.queued_block(
                self.client, self.body, 118, self.member.validator_hotkey
            )

    async def test_identity_or_epoch_drift_refuses_at_actual_queued_task(self):
        for name, changed in (
            ("Uids", 1),
            ("Keys", self.other),
            ("Owner", self.other),
            ("SubnetOwner", self.pin.policy.collector_coldkey),
            ("SubnetEpochIndex", 10),
            ("LastEpochBlock", 102),
            ("Uids", None),
        ):
            with self.subTest(name=name, changed=changed):
                old = self.values[name]
                self.values[name] = changed

                async def guard(body, netuid, hotkey):
                    return await treasury.queued_block(
                        self.client, body, netuid, hotkey
                    )

                contact = SimpleNamespace(
                    hotkey=self.member.validator_hotkey,
                    guard_treasury_dispatch=AsyncMock(side_effect=guard),
                    get_hyperparams=AsyncMock(),
                    get_neurons_list=AsyncMock(),
                    set_weights=AsyncMock(),
                    commit_weights=AsyncMock(),
                )
                task = tasks.ApplyWeights(
                    SimpleNamespace(identity_name="synthetic"),
                    contact,
                    self.body["weights"],
                    118,
                    0,
                )
                with (
                    patch.object(
                        receipts,
                        "treasury_task_body",
                        return_value=(self.body, self.member.validator_hotkey),
                    ),
                    self.assertRaises(tasks.StopRetrying),
                ):
                    await task._apply_weights(
                        SimpleNamespace(number=101, hash=self.pin.pinned_block_hash)
                    )
                contact.get_hyperparams.assert_not_awaited()
                contact.get_neurons_list.assert_not_awaited()
                contact.set_weights.assert_not_awaited()
                contact.commit_weights.assert_not_awaited()
                self.values[name] = old

    async def test_missing_configuration_has_no_capability_or_queued_submission(self):
        with patch.dict(os.environ, {"DITTO_TREASURY_SHADOW_APPROVAL_FILE": ""}):
            self.assertIsNone(treasury.capability())
            with self.assertRaises(tasks.StopRetrying):
                await treasury.queued_block(
                    self.client, self.body, 118, self.member.validator_hotkey
                )
        self.client.subtensor.rpc.assert_not_awaited()

    async def test_precommit_rechecks_identity_after_successful_queue_check(self):
        await treasury.queued_block(
            self.client, self.body, 118, self.member.validator_hotkey
        )
        self.values["Keys"] = self.other
        with (
            patch.object(
                receipts,
                "treasury_task_body",
                return_value=(self.body, self.member.validator_hotkey),
            ),
            self.assertRaises(tasks.StopRetrying),
        ):
            await treasury.guard_normalized_commit(self.client, 118, {0: 100, 1: 900})
        self.assertEqual(self.client.subtensor.rpc.await_count, 2)

    async def test_actual_turbobt_commit_refuses_uid_reuse_before_extrinsic(self):
        await treasury.queued_block(
            self.client, self.body, 118, self.member.validator_hotkey
        )
        self.values.update(
            {"PendingEpochAt": 0, "Tempo": 360, "BlocksSinceLastStep": 2}
        )
        head = self.head

        class BlockContext:
            number = 102
            hash = head

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        self.client.blocks = {-1: BlockContext()}
        self.client.wallet = SimpleNamespace(
            hotkey=SimpleNamespace(public_key=bytes(32))
        )
        submit = AsyncMock()
        self.client.subtensor.subtensor_module = SimpleNamespace(
            commit_timelocked_mechanism_weights=submit
        )
        subnet = SimpleNamespace(
            netuid=118,
            client=self.client,
            get_hyperparameters=AsyncMock(
                return_value={"tempo": 360, "commit_reveal_period": 1}
            ),
        )

        def encrypt(*_args, **_kwargs):
            # Queue guard succeeded, but UID ownership changes before commit.
            self.values["Keys"] = self.other
            return b"ciphertext", 123

        with (
            patch("bittensor_drand.get_encrypted_commit_v2", side_effect=encrypt),
            patch.object(
                receipts,
                "treasury_task_body",
                return_value=(self.body, self.member.validator_hotkey),
            ),
            self.assertRaises(tasks.StopRetrying),
        ):
            await SubnetWeights(subnet).commit({0: 0.1, 1: 0.9}, mechanism_id=0)
        submit.assert_not_awaited()
        self.assertEqual(self.client.subtensor.rpc.await_count, 2)

    async def test_unsupported_backend_guard_refuses(self):
        with self.assertRaises(tasks.StopRetrying):
            await AbstractBittensorContact.guard_treasury_dispatch(
                None, self.body, 118, self.member.validator_hotkey
            )

    def test_paused_pool_refuses_even_small_positive_collector_weight(self):
        paused_policy = self.pin.policy.model_copy(update={"buckets": ()})
        # Pure allocation check receives an already verified pin in production.
        paused = self.pin.model_copy(update={"policy": paused_policy})
        treasury.validate_normalized_service_vector(
            paused, collector_uid=0, weights={1: 65535}
        )
        with self.assertRaises(ValueError):
            treasury.validate_normalized_service_vector(
                paused, collector_uid=0, weights={0: 1, 1: 65535}
            )

    def test_normalized_rounding_keeps_service_pool_and_refuses_loss(self):
        # The installed normalizer can round a 10% share to either adjacent
        # integer. Both conserve that pool within the bounded quantization
        # allowance; a material reduction or increase must refuse.
        for collector in (6553, 6554):
            treasury.validate_normalized_service_vector(
                self.pin,
                collector_uid=0,
                weights={0: collector, 1: 65535 - collector},
            )
        for collector in (0, 6546, 6561):
            with self.subTest(collector=collector), self.assertRaises(ValueError):
                treasury.validate_normalized_service_vector(
                    self.pin,
                    collector_uid=0,
                    weights={0: collector, 1: 65535 - collector},
                )
        small_policy = self.pin.policy.model_copy(
            update={
                "buckets": (
                    self.pin.policy.buckets[0].model_copy(update={"allocation_bps": 1}),
                )
            }
        )
        small = self.pin.model_copy(update={"policy": small_policy})
        treasury.validate_normalized_service_vector(
            small, collector_uid=0, weights={0: 1, 1: 9999}
        )
        with self.assertRaises(ValueError):
            treasury.validate_normalized_service_vector(
                small, collector_uid=0, weights={1: 10000}
            )


if __name__ == "__main__":
    unittest.main()
