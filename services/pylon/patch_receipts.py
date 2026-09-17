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
            "        async def recorded_apply():\n            async with record_task(self._task_id) as should_submit:\n                if should_submit:\n                    await self._apply_weights(latest_block)\n        await asyncio.wait_for(asyncio.shield(recorded_apply()), 120)\n",
        )
    elif name == "turbobt":
        source = replace_once(
            source,
            "        extrinsic = await self.client.subtensor.subtensor_module.commit_timelocked_mechanism_weights(\n",
            "        from ditto_pylon_receipts import prepare_commit\n        await prepare_commit(\n            self.subnet.netuid, mechanism_id, dict(zip(uids, weights)),\n            bytes(commit), reveal_round, version_key,\n        )\n        extrinsic = await self.client.subtensor.subtensor_module.commit_timelocked_mechanism_weights(\n",
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
            "import structlog\nfrom ditto_pylon_receipt_api import (put_weight_receipt, get_weight_receipt, list_weight_receipts, acknowledge_weight_receipt)\n",
        )
        source = replace_once(
            source,
            "class IdentityController(Controller):\n",
            "class IdentityController(Controller):\n    put_weight_receipt = put_weight_receipt\n    get_weight_receipt = get_weight_receipt\n    list_weight_receipts = list_weight_receipts\n    acknowledge_weight_receipt = acknowledge_weight_receipt\n",
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
    }
    outputs = {name: patch(name, path.read_text()) for name, path in paths.items()}
    for name, source in outputs.items():
        paths[name].write_text(source)
    own = Path(__file__).parent
    for name in ("ditto_pylon_receipts.py", "ditto_pylon_receipt_api.py"):
        (site / name).write_bytes((own / name).read_bytes())
    (service / "db/migrations/versions/ditto_receipts_v1.py").write_bytes(
        (own / "receipt_migration.py").read_bytes()
    )


if __name__ == "__main__":
    main()
