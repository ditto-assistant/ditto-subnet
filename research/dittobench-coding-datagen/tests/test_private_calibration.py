from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.cli import main
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_calibration import (
    compile_private_calibration,
    load_private_calibration,
)
from dittobench_coding_datagen.private_group import (
    PrivateGroupArm,
    build_private_group_manifest,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest():  # type: ignore[no-untyped-def]
    return build_private_group_manifest(
        opaque_group_id="private-group-01",
        opaque_repository_stratum_id="private-stratum-01",
        repository_epoch="private-epoch-01",
        snapshot_manifest_sha256=_sha("snapshot"),
        visible_issue_sha256=_sha("issue"),
        runtime_policy_sha256=_sha("runtime"),
        hidden_grader_sha256=_sha("grader"),
        resource_profile_sha256=_sha("resource"),
        arms=tuple(
            PrivateGroupArm(
                condition=condition,  # type: ignore[arg-type]
                memory_bundle_sha256=_sha(condition),
                seeded_memory_bytes=4096,
                memory_volume_tier="small",
            )
            for condition in (
                "v0_none",
                "v1_relevant",
                "v2_irrelevant",
                "v3_stale_conflict",
                "v4_current_override",
            )
        ),
    )


def _observation(
    path: Path,
    *,
    manifest_sha256: str,
    candidate: str,
    replicate: int,
    hidden_passed: int,
    runner_profile_sha256: str | None = None,
) -> Path:
    path.write_bytes(
        canonical_json_bytes(
            {
                "build_passed": True,
                "candidate": candidate,
                "group_manifest_sha256": manifest_sha256,
                "hidden_tests_passed": hidden_passed,
                "hidden_tests_total": 3,
                "replicate_id": f"replicate-{replicate:02d}",
                "runner_profile_sha256": runner_profile_sha256
                or _sha("runner-profile"),
                "schema": "dittobench-coding-private-calibration-observation-v2",
                "visible_tests_passed": 2,
                "visible_tests_total": 2,
            }
        )
    )
    return path


def _observations(root: Path) -> tuple[Path, ...]:
    manifest_sha256 = _manifest().manifest_sha256()
    return tuple(
        _observation(
            root / f"{candidate}-{replicate}.json",
            manifest_sha256=manifest_sha256,
            candidate=candidate,
            replicate=replicate,
            hidden_passed=0 if candidate == "base" else 3,
        )
        for candidate in ("base", "reference")
        for replicate in (1, 2)
    )


def test_private_calibration_compiles_repeated_outcomes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    manifest = _manifest()
    manifest_path = protected / "group-manifest.json"
    manifest_path.write_bytes(manifest.canonical_bytes())
    observations = _observations(protected)
    output = protected / "group-calibration.json"
    arguments = [
        "compile-private-calibration",
        "--manifest",
        str(manifest_path),
    ]
    for observation in observations:
        arguments.extend(("--observation", str(observation)))
    arguments.extend(("--output", str(output)))
    assert main(arguments) == 0
    compiled = json.loads(capsys.readouterr().out)
    assert compiled["passed"] is True
    assert compiled["replicate_count_per_candidate"] == 2
    assert compiled["weight_eligible"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert (
        load_private_calibration(
            output, group_manifest_sha256=manifest.manifest_sha256()
        )[0]
        == compiled
    )


def test_private_calibration_rejects_base_or_reference_without_delta(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    observations = list(_observations(tmp_path))
    raw = json.loads(observations[0].read_bytes())
    raw["hidden_tests_passed"] = 3
    observations[0].write_bytes(canonical_json_bytes(raw))
    with pytest.raises(CorpusError, match="base outcome|deterministic"):
        compile_private_calibration(manifest=manifest, observations=tuple(observations))


def test_private_calibration_rejects_runner_or_replicate_drift(tmp_path: Path) -> None:
    manifest = _manifest()
    observations = list(_observations(tmp_path))
    raw = json.loads(observations[-1].read_bytes())
    raw["runner_profile_sha256"] = _sha("other-runner")
    observations[-1].write_bytes(canonical_json_bytes(raw))
    with pytest.raises(CorpusError, match="runner profile"):
        compile_private_calibration(manifest=manifest, observations=tuple(observations))
