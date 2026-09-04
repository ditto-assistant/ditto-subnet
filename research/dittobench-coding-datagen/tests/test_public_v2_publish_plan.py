from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_public_v2_release import _pack

from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_v2_publish_plan import (
    build_public_v2_publish_plan,
)
from dittobench_coding_datagen.public_v2_release import build_public_v2_release


def _release(root: Path) -> Path:
    release = root / "release"
    build_public_v2_release(pack=_pack(root), output=release)
    return release


def test_public_v2_publish_plan_is_canonical_and_credential_free(
    tmp_path: Path,
) -> None:
    plan = build_public_v2_publish_plan(
        release_dir=_release(tmp_path),
        dataset_repository="ditto-assistant/coding-practice",
        revision="main",
    )

    assert plan["weight_eligible"] is False
    assert plan["upload_prefix"] == (
        f"releases/coding-public-v2/{plan['pack_manifest_sha256']}"
    )
    assert [item["remote_path"] for item in plan["files"]] == sorted(
        item["remote_path"] for item in plan["files"]
    )
    assert "token" not in json.dumps(plan).lower()
    assert "credential" not in json.dumps(plan).lower()


def test_public_v2_publish_plan_rejects_invalid_target(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="target"):
        build_public_v2_publish_plan(
            release_dir=_release(tmp_path),
            dataset_repository="invalid target",
            revision="main",
        )


def test_public_v2_publish_plan_cli_refuses_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "plan.json"
    assert (
        main(
            [
                "plan-public-v2-publication",
                "--release-dir",
                str(_release(tmp_path)),
                "--dataset-repository",
                "ditto-assistant/coding-practice",
                "--revision",
                "main",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == json.loads(output.read_bytes())
    assert (
        main(
            [
                "plan-public-v2-publication",
                "--release-dir",
                str(tmp_path / "release"),
                "--dataset-repository",
                "ditto-assistant/coding-practice",
                "--revision",
                "main",
                "--output",
                str(output),
            ]
        )
        == 2
    )
