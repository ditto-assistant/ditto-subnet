"""Build-time adaptation of exact pinned Pylon 2.3.2 and TurboBT source bytes.

Never silently patch a different upstream release. No runtime monkeypatching,
legacy scheduling fallback, wallet access, or change to weight normalization.
"""

# Exact upstream source anchors are intentionally kept on one line.
# ruff: noqa: E501

from __future__ import annotations

import ast
import hashlib
import sys
import sysconfig
from pathlib import Path

SOURCE_HASHES = {
    "services": "cd2f0f86acd28128b4240b1d17e7990510d7f2e305b9c7628534dc087b3bfd15",
    "tasks": "9e9ff0555459e44706a9290d7b7f14c7d14f627feec1ec39c3799e5da97226c8",
    "contact": "aeb08229f3150ff52c453d7dba44ee8903019a5c60b78e23d0a4a68d2791552f",
    "router": "d106969ff2d44573231f3dbc9ce86951a10c240777e963ed03cfbbdb4e3174d1",
    "turbobt": "840418d3d294558943584a08b24be9b144916b6bcd24e4bc5ec6eba4cd70c6bd",
}
DEPENDENCIES_HASH = "8078226057cc9523a5b07a3a14bdf320bef4cd5d16a4f7cd0f500ecea55560d9"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError("pinned epoch patch anchor differs")
    return source.replace(old, new, 1)


def patch_dependencies(source: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != DEPENDENCIES_HASH:
        raise ValueError("unreviewed TurboBT dependency manifest")
    source = replace_once(
        source, '"bittensor-drand~=1.0.0"', '"bittensor-drand==2.0.0"'
    )
    return replace_once(
        source, '"bittensor-wallet~=4.0.0"', '"bittensor-wallet==4.1.1"'
    )


def patch(name: str, source: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_HASHES[name]:
        raise ValueError(f"unreviewed {name} source; refusing epoch adaptation")
    if name == "turbobt":
        source = replace_once(
            source,
            "from __future__ import annotations\n",
            "from __future__ import annotations\n"
            "from ditto_pylon_epoch import read_epoch_schedule\n",
        )
        source = replace_once(
            source,
            "            commit, reveal_round = bittensor_drand.get_encrypted_commit(\n",
            "            schedule = await read_epoch_schedule(\n"
            "                self.client, self.subnet.netuid, block.number, block.hash\n"
            "            )\n"
            '            if schedule.tempo != hyperparameters["tempo"]:\n'
            '                raise ValueError("epoch and hyperparameter tempo differ")\n'
            "            commit, reveal_round = bittensor_drand.get_encrypted_commit_v2(\n",
        )
        source = replace_once(
            source,
            '                tempo=hyperparameters["tempo"],\n'
            "                current_block=block.number,\n"
            "                netuid=storage_index,\n",
            "                **schedule.drand_arguments(),\n",
        )
        # Only the old timelock formula used this mechanism storage index.
        source = replace_once(
            source,
            "            storage_index = _get_mechid_storage_index(self.subnet.netuid, mechanism_id)\n",
            "",
        )
    elif name == "contact":
        source = replace_once(
            source,
            "from __future__ import annotations\n",
            "from __future__ import annotations\n"
            "from ditto_pylon_epoch import EpochSchedule, read_epoch_schedule\n",
        )
        # The concrete adapter supplies this additional internal read. The
        # existing router's _delegate retains normal reconnect/archive handling.
        source = replace_once(
            source,
            "    async def get_neurons_list(self, netuid: NetUid, block: Block) -> list[Neuron]: ...\n"
            "    async def get_hyperparams(self, netuid: NetUid, block: Block) -> SubnetHyperparams | None: ...\n"
            "    async def get_certificates(self, netuid: NetUid, block: Block) -> dict[Hotkey, NeuronCertificate]: ...\n",
            "    async def get_neurons_list(self, netuid: NetUid, block: Block) -> list[Neuron]: ...\n"
            "    async def get_epoch_schedule(\n"
            "        self, netuid: NetUid, block: Block\n"
            "    ) -> EpochSchedule: ...\n\n"
            "    async def get_hyperparams(self, netuid: NetUid, block: Block) -> SubnetHyperparams | None: ...\n"
            "    async def get_certificates(self, netuid: NetUid, block: Block) -> dict[Hotkey, NeuronCertificate]: ...\n",
        )
        source = replace_once(
            source,
            "    @abstractmethod\n"
            "    async def get_hyperparams(self, netuid: NetUid, block: Block) -> SubnetHyperparams | None: ...\n",
            "    @abstractmethod\n"
            "    async def get_epoch_schedule(\n"
            "        self, netuid: NetUid, block: Block\n"
            "    ) -> EpochSchedule: ...\n\n"
            "    @abstractmethod\n"
            "    async def get_hyperparams(self, netuid: NetUid, block: Block) -> SubnetHyperparams | None: ...\n",
        )
        source = replace_once(
            source,
            "class TurboBtContact(AbstractBittensorContact):\n",
            "class TurboBtContact(AbstractBittensorContact):\n"
            "    async def get_epoch_schedule(self, netuid, block) -> EpochSchedule:\n"
            "        return await self._protect_turbobt(\n"
            '            "get_epoch_schedule",\n'
            "            lambda c: read_epoch_schedule(c, netuid, block.number, block.hash),\n"
            "        )\n\n",
        )
    elif name == "router":
        source = replace_once(
            source,
            "    async def get_hyperparams(self, netuid: NetUid, block: Block) -> SubnetHyperparams | None:\n",
            "    async def get_epoch_schedule(self, netuid, block):\n"
            "        return await self._delegate(\n"
            '            "get_epoch_schedule",\n'
            "            main_call=lambda: self._main_contact.get_epoch_schedule(netuid, block),\n"
            "            archive_call=lambda: self._archive_contact.get_epoch_schedule(netuid, block),\n"
            "            block=block,\n"
            "        )\n\n"
            "    async def get_hyperparams(self, netuid: NetUid, block: Block) -> SubnetHyperparams | None:\n",
        )
    elif name == "services":
        source = replace_once(
            source,
            "from pylon_service.api.epoch import get_epoch_containing_block, get_tempo_from_hyperparams\n",
            "from pylon_service.api.epoch import Epoch\n",
        )
        source = replace_once(
            source,
            "        hyperparams = await self.contact_router.get_hyperparams(netuid, block)\n"
            "        tempo = get_tempo_from_hyperparams(hyperparams)\n"
            "        epoch = get_epoch_containing_block(block_number, netuid, tempo)\n",
            "        schedule = await self.contact_router.get_epoch_schedule(netuid, block)\n"
            "        epoch = Epoch(\n"
            "            start=schedule.last_epoch_block,\n"
            "            end=max(block_number, schedule.next_epoch_block - 1),\n"
            "        )\n",
        )
    else:
        source = replace_once(
            source,
            "from pylon_service.api.epoch import Epoch, get_epoch_containing_block, get_tempo_from_hyperparams\n",
            "from pylon_service.api.epoch import Epoch\n",
        )
        source = replace_once(
            source,
            "        self._initial_tempo: Epoch | None = None\n",
            "        self._initial_tempo: Epoch | None = None\n"
            "        self._initial_epoch_index: int | None = None\n",
        )
        source = replace_once(
            source,
            "        hyperparams = await self._client.get_hyperparams(self._netuid, start_block)\n"
            "        tempo = get_tempo_from_hyperparams(hyperparams)\n"
            "        self._initial_tempo = get_epoch_containing_block(self._start_block_number, self._netuid, tempo)\n",
            "        schedule = await self._client.get_epoch_schedule(self._netuid, start_block)\n"
            "        self._initial_epoch_index = schedule.subnet_epoch_index\n"
            "        self._initial_tempo = Epoch(\n"
            "            start=schedule.last_epoch_block,\n"
            "            end=max(self._start_block_number, schedule.next_epoch_block - 1),\n"
            "        )\n",
        )
        source = replace_once(
            source,
            "        if latest_block.number > self._initial_tempo.end:\n",
            "        schedule = await self._client.get_epoch_schedule(self._netuid, latest_block)\n"
            "        if (schedule.subnet_epoch_index != self._initial_epoch_index\n"
            "                or latest_block.number < self._start_block_number):\n",
        )
        source = replace_once(
            source,
            "        remaining = self._initial_tempo.end - latest_block.number\n",
            "        remaining = max(0, schedule.next_epoch_block - 1 - latest_block.number)\n",
        )
    ast.parse(source)
    compile(source, f"<patched-{name}>", "exec")
    return source


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--dependencies":
        manifest = Path(sys.argv[2])
        manifest.write_text(patch_dependencies(manifest.read_text()))
        return
    if len(sys.argv) != 1:
        raise ValueError("invalid epoch adaptation arguments")
    site = Path(sysconfig.get_paths()["purelib"])
    service = Path("/app/pylon_service/pylon_service")
    paths = {
        "services": service / "api/_unstable/services.py",
        "tasks": service / "api/_unstable/tasks.py",
        "contact": service / "bittensor/contact.py",
        "router": service / "bittensor/contact_router.py",
        "turbobt": site / "turbobt/subnet.py",
    }
    # Validate every input before writing any adapted source.
    patched = {name: patch(name, path.read_text()) for name, path in paths.items()}
    for name, source in patched.items():
        paths[name].write_text(source)
    helper = Path(__file__).with_name("ditto_pylon_epoch.py")
    (site / helper.name).write_bytes(helper.read_bytes())


if __name__ == "__main__":
    main()
