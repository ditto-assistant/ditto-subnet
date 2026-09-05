"""Grade ten miner-owned workspaces into a non-authoritative practice report."""

from __future__ import annotations

import json
from pathlib import Path

from dittobench_coding_datagen.canonical import sha256_hex
from dittobench_coding_datagen.local_result import build_local_practice_result
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import validate_public_v2_pack
from dittobench_coding_datagen.public_task_runner import run_public_v2_task


def run_public_practice(
    *, pack: Path, workspaces: Path, images: Path, harness_artifact_sha256: str
) -> bytes:
    manifest = validate_public_v2_pack(pack)
    if images.is_symlink() or not images.is_file() or images.stat().st_size > 65536:
        raise CorpusError("public runtime image map is invalid")
    try:
        runtime_images = json.loads(images.read_bytes())
        entries = [
            json.loads(line)
            for line in (pack / "tasks/index.jsonl").read_bytes().splitlines()
        ]
    except (ValueError, OSError) as error:
        raise CorpusError("public practice inputs are invalid") from error
    if not isinstance(runtime_images, dict) or set(runtime_images) != {
        e["task_id"] for e in entries
    }:
        raise CorpusError("public runtime image map must cover exactly ten tasks")
    if any(
        not isinstance(image, str) or "@sha256:" not in image
        for image in runtime_images.values()
    ):
        raise CorpusError("public runtime images must be pinned by digest")
    # Validate the caller's report identity before starting any container.
    if len(harness_artifact_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in harness_artifact_sha256
    ):
        raise CorpusError("harness artifact SHA-256 is invalid")
    for entry in entries:
        workspace = workspaces / entry["task_id"] / "workspace"
        if workspace.is_symlink() or not workspace.is_dir():
            raise CorpusError("prepare every public workspace before grading")
    tasks = tuple(
        run_public_v2_task(
            pack=pack,
            task_id=entry["task_id"],
            workspace=workspaces / entry["task_id"] / "workspace",
            image=runtime_images[entry["task_id"]],
        )
        for entry in entries
    )
    return build_local_practice_result(
        public_release_id=manifest["public_release_id"],
        public_release_manifest_sha256=sha256_hex(
            (pack / "manifest.json").read_bytes()
        ),
        harness_artifact_sha256=harness_artifact_sha256,
        tasks=tasks,
    ).canonical_bytes()
