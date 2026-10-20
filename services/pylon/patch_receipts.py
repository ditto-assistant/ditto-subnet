"""Exact-source build-time extension for Pylon 2.3.2 weight receipts.

Run after patch_epoch_schedule.py. Inputs are pinned original hashes transformed
by that reviewed patch, not guessed text from arbitrary upstream releases.
"""

# Exact source anchors intentionally remain on one line.
# ruff: noqa: E501
from __future__ import annotations

import ast
import hashlib
import sysconfig
from pathlib import Path

from patch_epoch_schedule import replace_once

HASHES: dict[str, str] = {
    "tasks": "d7d7b60045258cff798a9390745e76c502d5a95b6d5e27bbedb53ae71f359b52",
    "turbobt": "97feaf773350d1ae0d2a6335f26dbaf293a97d03139af210175654f7641017aa",
    "api": "f6bdba992214c7d9ef4cb09a8bda09b1510ca18b8d4425ab2249048143c386aa",
    "extrinsic": "472110718d8aaf3d1e3a37cbace73f18204439e471f40620667675be0db94b55",
    "contact": "f7fe5e3e7182bc0349bdd898d0befbd989b844e78d83ba3e5355acced6686dc2",
    "router": "e1f5aad812e97ae85c4dca7892bab53cfc0e9798c6cfe84f75e163bd55612daf",
}


def patch(name: str, source: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != HASHES[name]:
        raise ValueError(f"unreviewed {name} receipt input")
    if name == "tasks":
        source = replace_once(
            source,
            "import asyncio\n",
            "import asyncio\nfrom ditto_pylon_receipts import record_task\n",
        )
        source = replace_once(
            source,
            "        await asyncio.wait_for(asyncio.shield(self._apply_weights(latest_block)), 120)\n",
            "        async def recorded_apply():\n            async with record_task(self._task_id, self._hotkey) as should_submit:\n                if should_submit:\n                    await self._apply_weights(latest_block)\n        await asyncio.wait_for(asyncio.shield(recorded_apply()), 120)\n",
        )
        source = replace_once(
            source,
            '        logger.info("applying_weights")\n',
            '        logger.info("applying_weights")\n'
            "        from ditto_pylon_receipts import treasury_task_body\n"
            "        from ditto_pylon_treasury import require_legacy_dispatch_allowed\n"
            "        treasury_body, _ = treasury_task_body()\n"
            "        if treasury_body is None:\n"
            "            require_legacy_dispatch_allowed()\n"
            "        if treasury_body is not None:\n"
            "            latest_block = await self._client.guard_treasury_dispatch(\n"
            "                treasury_body, self._netuid, self._hotkey\n"
            "            )\n",
        )
        source = replace_once(
            source,
            "        commit_reveal_enabled = hyperparams.commit_reveal_weights_enabled\n",
            "        commit_reveal_enabled = hyperparams.commit_reveal_weights_enabled\n"
            "        if treasury_body is not None and not commit_reveal_enabled:\n"
            '            raise StopRetrying("treasury requires guarded commit transport")\n',
        )
    elif name == "turbobt":
        source = replace_once(
            source,
            "        extrinsic = await self.client.subtensor.subtensor_module.commit_timelocked_mechanism_weights(\n",
            "        from ditto_pylon_treasury import guard_normalized_commit\n        await guard_normalized_commit(self.client, self.subnet.netuid, dict(zip(uids, weights)))\n        from ditto_pylon_receipts import prepare_commit\n        await prepare_commit(\n            self.subnet.netuid, mechanism_id, dict(zip(uids, weights)),\n            bytes(commit), reveal_round, version_key,\n        )\n        extrinsic = await self.client.subtensor.subtensor_module.commit_timelocked_mechanism_weights(\n",
        )
    elif name == "extrinsic":
        source = replace_once(
            source,
            "            except StopIteration:\n                return\n",
            '            except StopIteration:\n                from ditto_pylon_receipts import finalize_commit\n                await finalize_commit(status["finalized"], block, extrinsic_hash, extrinsic_idx, events)\n                return\n',
        )
    elif name == "api":
        source = replace_once(
            source,
            "import structlog\n",
            "import structlog\nfrom ditto_pylon_receipt_api import (put_weight_receipt, get_weight_receipt, list_weight_receipts, acknowledge_weight_receipt, treasury_capability)\n",
        )
        source = replace_once(
            source,
            "class IdentityController(Controller):\n",
            "class IdentityController(Controller):\n    put_weight_receipt = put_weight_receipt\n    get_weight_receipt = get_weight_receipt\n    list_weight_receipts = list_weight_receipts\n    acknowledge_weight_receipt = acknowledge_weight_receipt\n    treasury_capability = treasury_capability\n",
        )
    elif name == "contact":
        source = replace_once(
            source,
            "class BittensorPort(Protocol):\n",
            "class BittensorPort(Protocol):\n"
            "    async def guard_treasury_dispatch(self, body, netuid, validator_hotkey) -> Block: ...\n\n",
        )
        source = replace_once(
            source,
            "class AbstractBittensorContact(BittensorPort, ABC):\n",
            "class AbstractBittensorContact(BittensorPort, ABC):\n"
            "    async def guard_treasury_dispatch(self, body, netuid, validator_hotkey):\n"
            "        from pylon_service.api._unstable.tasks import StopRetrying\n"
            '        raise StopRetrying("treasury guard is unavailable on this backend")\n\n',
        )
        source = replace_once(
            source,
            "class TurboBtContact(AbstractBittensorContact):\n",
            "class TurboBtContact(AbstractBittensorContact):\n"
            "    async def guard_treasury_dispatch(self, body, netuid, validator_hotkey):\n"
            "        from ditto_pylon_treasury import queued_block\n"
            "        return await self._protect_turbobt(\n"
            '            "guard_treasury_dispatch",\n'
            "            lambda c: queued_block(c, body, netuid, validator_hotkey),\n"
            "        )\n\n",
        )
    elif name == "router":
        source = replace_once(
            source,
            "    async def get_epoch_schedule(self, netuid, block):\n",
            "    async def guard_treasury_dispatch(self, body, netuid, validator_hotkey):\n"
            "        return await self._delegate(\n"
            '            "guard_treasury_dispatch",\n'
            "            main_call=lambda: self._main_contact.guard_treasury_dispatch(body, netuid, validator_hotkey),\n"
            "            archive_call=lambda: self._archive_contact.guard_treasury_dispatch(body, netuid, validator_hotkey),\n"
            "        )\n\n"
            "    async def get_epoch_schedule(self, netuid, block):\n",
        )
    else:
        raise ValueError(name)
    ast.parse(source)
    compile(source, f"<receipt-{name}>", "exec")
    return source


def main() -> None:
    site = Path(sysconfig.get_paths()["purelib"])
    service = Path("/app/pylon_service/pylon_service")
    paths = {
        "tasks": service / "api/_unstable/tasks.py",
        "api": service / "api/_unstable/api.py",
        "turbobt": site / "turbobt/subnet.py",
        "extrinsic": site / "turbobt/substrate/extrinsic.py",
        "contact": service / "bittensor/contact.py",
        "router": service / "bittensor/contact_router.py",
    }
    outputs = {name: patch(name, path.read_text()) for name, path in paths.items()}
    for name, source in outputs.items():
        paths[name].write_text(source)
    own = Path(__file__).parent
    for name in (
        "ditto_pylon_receipts.py",
        "ditto_pylon_receipt_api.py",
        "ditto_pylon_treasury.py",
    ):
        (site / name).write_bytes((own / name).read_bytes())
    (service / "db/migrations/versions/ditto_receipts_v1.py").write_bytes(
        (own / "receipt_migration.py").read_bytes()
    )


if __name__ == "__main__":
    main()
