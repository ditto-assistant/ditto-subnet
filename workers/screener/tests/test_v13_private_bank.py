"""Synthetic protected-bank ingress tests; no production case material."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from multiprocessing.connection import Connection
from pathlib import Path
from uuid import uuid4

import pytest

from ditto_screener.v13_private_bank import FileProtectedBlueprintBank
from ditto_screening_protocol.v13_private_provision import (
    PrivateProvisioningUnavailable,
)


def _load_nonregular_bank(root: Path, ready: Connection) -> None:
    import asyncio

    ready.send(True)
    ready.close()
    try:
        asyncio.run(FileProtectedBlueprintBank(root).load())
    except PrivateProvisioningUnavailable:
        return
    raise AssertionError("nonregular bank file was accepted")


def _write_bank(root: Path, *, mismatched_semantics: bool = False) -> None:
    root.mkdir(mode=0o700)
    payloads = root / "payloads"
    payloads.mkdir(mode=0o700)
    pair_id = uuid4()
    digests = {}
    for side in ("control", "variant"):
        raw = json.dumps(
            {
                "revision": "v13-private-case-v1",
                "pair_id": str(pair_id),
                "side": side,
                "semantic_contract_sha256": (
                    "b" * 64 if side == "variant" and mismatched_semantics else "a" * 64
                ),
                "seed_envelope": None,
                "run_envelope": {"prompt": "synthetic"},
                "expected_answer_sha256": "c" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = payloads / digest
        path.write_bytes(raw)
        path.chmod(0o600)
        digests[side] = digest
    index = root / "bank.json"
    index.write_text(
        json.dumps(
            {
                "revision": "v13-protected-blueprint-bank-v1",
                "pairs": [
                    {
                        "pair_id": str(pair_id),
                        "transformation_class": "field_entity_rename",
                        "control_sha256": digests["control"],
                        "variant_sha256": digests["variant"],
                    }
                ],
            }
        )
    )
    index.chmod(0o600)


@pytest.mark.asyncio
async def test_protected_bank_loads_digest_bound_pair(tmp_path: Path) -> None:
    root = tmp_path / "bank"
    _write_bank(root)
    pairs = await FileProtectedBlueprintBank(root).load()
    assert len(pairs) == 1
    assert pairs[0].transformation_class == "field_entity_rename"
    assert pairs[0].control != pairs[0].variant


@pytest.mark.asyncio
async def test_protected_bank_fails_closed_on_permissions_and_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bank"
    _write_bank(root)
    root.chmod(0o755)
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(root).load()
    root.chmod(0o700)
    index = root / "bank.json"
    index.rename(root / "real.json")
    index.symlink_to(root / "real.json")
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(root).load()
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(None).load()


@pytest.mark.asyncio
async def test_protected_bank_rejects_semantic_mismatch_and_tamper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bank"
    _write_bank(root, mismatched_semantics=True)
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(root).load()
    root2 = tmp_path / "bank2"
    _write_bank(root2)
    payload = next((root2 / "payloads").iterdir())
    payload.write_bytes(b"tampered")
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(root2).load()


@pytest.mark.asyncio
async def test_protected_bank_rejects_readable_payload(tmp_path: Path) -> None:
    root = tmp_path / "bank"
    _write_bank(root)
    payload = next((root / "payloads").iterdir())
    payload.chmod(0o644)
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(root).load()


@pytest.mark.asyncio
async def test_protected_bank_checks_permissions_on_opened_payload_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bank"
    _write_bank(root)
    payload = next((root / "payloads").iterdir())
    original_open = os.open

    def chmod_after_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == payload.name:
            payload.chmod(0o644)
        return fd

    monkeypatch.setattr(os, "open", chmod_after_open)
    with pytest.raises(PrivateProvisioningUnavailable):
        await FileProtectedBlueprintBank(root).load()


@pytest.mark.asyncio
async def test_protected_bank_reads_opened_file_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bank"
    _write_bank(root)
    original_open = os.open

    def swap_after_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == "bank.json":
            (root / "bank.json").rename(root / "original.json")
            replacement = root / "bank.json"
            replacement.write_text("{}")
            replacement.chmod(0o644)
        return fd

    monkeypatch.setattr(os, "open", swap_after_open)
    pairs = await FileProtectedBlueprintBank(root).load()
    assert len(pairs) == 1


@pytest.mark.parametrize("index", [True, False], ids=["index", "payload"])
def test_protected_bank_rejects_fifo_without_waiting_for_a_writer(
    tmp_path: Path, index: bool
) -> None:
    root = tmp_path / "bank"
    _write_bank(root)
    path = root / "bank.json" if index else next((root / "payloads").iterdir())
    path.unlink()
    os.mkfifo(path, mode=0o600)
    # Bound the child as well as the async operation: a thread blocked in
    # open() cannot be cancelled by asyncio and would otherwise hang pytest.
    context = multiprocessing.get_context("spawn")
    ready, child_ready = context.Pipe(duplex=False)
    process = context.Process(target=_load_nonregular_bank, args=(root, child_ready))
    process.start()
    child_ready.close()
    try:
        # Starting a fresh interpreter can be slow under the full suite. Start
        # the I/O deadline only after the child has imported the loader.
        assert ready.poll(30), "bank loader child did not start"
        assert ready.recv() is True
        process.join(timeout=5)
        assert not process.is_alive(), "bank loader blocked opening a FIFO"
        assert process.exitcode == 0
    finally:
        ready.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
