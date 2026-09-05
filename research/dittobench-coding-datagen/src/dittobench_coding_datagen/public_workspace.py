"""Prepare public task inputs for a miner-owned local editing session."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from dittobench_coding_datagen.canonical import safe_relative_path
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack


def prepare_public_workspace(
    *, pack: Path, task_id: str, output: Path
) -> dict[str, object]:
    manifest = validate_public_v2_pack(pack)
    task_id = safe_relative_path(task_id)
    entries = [
        json.loads(line)
        for line in (pack / "tasks/index.jsonl").read_bytes().splitlines()
    ]
    if task_id not in {entry["task_id"] for entry in entries}:
        raise CorpusError("unknown public practice task")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise CorpusError("public workspace output must be new with an existing parent")
    output.mkdir()
    shutil.copytree(
        pack / "capsules" / task_id / "visible/workspace", output / "workspace"
    )
    for name in ("issue.json", "memory.json", "runtime-policy.json"):
        shutil.copyfile(pack / "tasks" / task_id / name, output / name)
    return {
        "public_release_id": manifest["public_release_id"],
        "task_id": task_id,
        "authoritative": False,
        "leaderboard_eligible": False,
        "reward_eligible": False,
    }
