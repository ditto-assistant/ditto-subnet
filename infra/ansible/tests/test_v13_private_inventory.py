from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "scripts" / "check-v13-private-inventory.py"
spec = importlib.util.spec_from_file_location("v13_private_inventory", SOURCE)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _valid_inventory() -> dict[str, object]:
    return {
        "role_v13_private_verifier": {"hosts": ["ditto-v13-private-verifier-prod"]},
        "_meta": {
            "hostvars": {
                "ditto-v13-private-verifier-prod": {
                    "labels": {"role": "v13_private_verifier", "managed": "terraform"}
                }
            }
        },
    }


def test_exact_host_group_and_labels_are_required() -> None:
    valid = _valid_inventory()
    module.validate_inventory(valid)
    for invalid in (
        {},
        {**valid, "role_v13_private_verifier": {"hosts": []}},
        {**valid, "role_v13_private_verifier": {"hosts": ["another-host"]}},
        {**valid, "_meta": {"hostvars": {}}},
    ):
        with pytest.raises(ValueError):
            module.validate_inventory(invalid)


def test_inventory_filename_is_recognized_by_gcp_plugin() -> None:
    assert module.INVENTORY.name.endswith("gcp_compute.yml")
    assert module.INVENTORY.exists()


def test_stage_preflight_runs_dynamic_parser_and_rejects_empty_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def parse_inventory(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"_meta": {"hostvars": {}}}), ""
        )

    monkeypatch.setattr(module.subprocess, "run", parse_inventory)
    assert module.main() == 1
    assert calls == [["ansible-inventory", "-i", str(module.INVENTORY), "--list"]]

    def parse_exact(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, json.dumps(_valid_inventory()), ""
        )

    monkeypatch.setattr(module.subprocess, "run", parse_exact)
    assert module.main() == 0
