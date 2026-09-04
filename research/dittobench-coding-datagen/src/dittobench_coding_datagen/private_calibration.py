"""Canonical repeated calibration evidence for private Coding groups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_opaque_id,
    sha256_hex,
)
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.private_group import PrivateGroupManifest

PRIVATE_CALIBRATION_OBSERVATION_SCHEMA = (
    "dittobench-coding-private-calibration-observation-v2"
)
PRIVATE_CALIBRATION_SCHEMA = "dittobench-coding-private-calibration-v2"
_MAX_OBSERVATION_BYTES = 1 << 20
_REPLICATES = 2


def compile_private_calibration(
    *, manifest: PrivateGroupManifest, observations: tuple[Path, ...]
) -> dict[str, Any]:
    """Require two stable base and reference observations for one group."""

    parsed = [_load_observation(path, manifest=manifest) for path in observations]
    if len(parsed) != _REPLICATES * 2:
        raise CorpusError("private calibration requires four observations")
    by_candidate = {
        candidate: sorted(
            (item for item in parsed if item[0]["candidate"] == candidate),
            key=lambda item: str(item[0]["replicate_id"]),
        )
        for candidate in ("base", "reference")
    }
    if any(len(items) != _REPLICATES for items in by_candidate.values()):
        raise CorpusError("private calibration requires two candidate replicates")
    if any(
        len({str(item[0]["replicate_id"]) for item in items}) != _REPLICATES
        for items in by_candidate.values()
    ):
        raise CorpusError("private calibration replicate identities are not unique")
    profiles = {str(item[0]["runner_profile_sha256"]) for item in parsed}
    if len(profiles) != 1:
        raise CorpusError("private calibration runner profile drifted")

    base_results = {_result_tuple(item[0]) for item in by_candidate["base"]}
    reference_results = {_result_tuple(item[0]) for item in by_candidate["reference"]}
    if len(base_results) != 1 or len(reference_results) != 1:
        raise CorpusError("private calibration replicates are not deterministic")
    base = by_candidate["base"][0][0]
    reference = by_candidate["reference"][0][0]
    if (
        not base["build_passed"]
        or base["visible_tests_passed"] != base["visible_tests_total"]
        or base["hidden_tests_passed"] >= base["hidden_tests_total"]
    ):
        raise CorpusError("private calibration base outcome is invalid")
    if (
        not reference["build_passed"]
        or reference["visible_tests_passed"] != reference["visible_tests_total"]
        or reference["hidden_tests_passed"] != reference["hidden_tests_total"]
        or (
            base["visible_tests_total"],
            base["hidden_tests_total"],
        )
        != (
            reference["visible_tests_total"],
            reference["hidden_tests_total"],
        )
    ):
        raise CorpusError("private calibration reference outcome is invalid")

    return {
        "base_observation_sha256": sorted(item[1] for item in by_candidate["base"]),
        "group_manifest_sha256": manifest.manifest_sha256(),
        "passed": True,
        "reference_observation_sha256": sorted(
            item[1] for item in by_candidate["reference"]
        ),
        "replicate_count_per_candidate": _REPLICATES,
        "runner_profile_sha256": profiles.pop(),
        "schema": PRIVATE_CALIBRATION_SCHEMA,
        "weight_eligible": False,
    }


def load_private_calibration(
    path: Path, *, group_manifest_sha256: str
) -> tuple[dict[str, Any], bytes]:
    """Load one canonical passing calibration receipt for release admission."""

    raw, body = _canonical_object(path, label="private calibration")
    expected = {
        "base_observation_sha256",
        "group_manifest_sha256",
        "passed",
        "reference_observation_sha256",
        "replicate_count_per_candidate",
        "runner_profile_sha256",
        "schema",
        "weight_eligible",
    }
    if (
        set(raw) != expected
        or raw["schema"] != PRIVATE_CALIBRATION_SCHEMA
        or raw["group_manifest_sha256"] != group_manifest_sha256
        or raw["passed"] is not True
        or raw["weight_eligible"] is not False
        or raw["replicate_count_per_candidate"] != _REPLICATES
        or not _sha256(raw["runner_profile_sha256"])
        or not _digest_list(raw["base_observation_sha256"])
        or not _digest_list(raw["reference_observation_sha256"])
    ):
        raise CorpusError("private calibration authority is invalid")
    return raw, body


def _load_observation(
    path: Path, *, manifest: PrivateGroupManifest
) -> tuple[dict[str, Any], str]:
    raw, body = _canonical_object(path, label="private calibration observation")
    expected = {
        "build_passed",
        "candidate",
        "group_manifest_sha256",
        "hidden_tests_passed",
        "hidden_tests_total",
        "replicate_id",
        "runner_profile_sha256",
        "schema",
        "visible_tests_passed",
        "visible_tests_total",
    }
    if (
        set(raw) != expected
        or raw["schema"] != PRIVATE_CALIBRATION_OBSERVATION_SCHEMA
        or raw["group_manifest_sha256"] != manifest.manifest_sha256()
        or raw["candidate"] not in {"base", "reference"}
        or not isinstance(raw["build_passed"], bool)
        or not _sha256(raw["runner_profile_sha256"])
    ):
        raise CorpusError("private calibration observation is invalid")
    try:
        safe_opaque_id(raw["replicate_id"])
    except (TypeError, ValueError) as error:
        raise CorpusError("private calibration observation is invalid") from error
    for passed_key, total_key in (
        ("visible_tests_passed", "visible_tests_total"),
        ("hidden_tests_passed", "hidden_tests_total"),
    ):
        passed = raw[passed_key]
        total = raw[total_key]
        if (
            type(passed) is not int
            or type(total) is not int
            or total < 1
            or passed < 0
            or passed > total
        ):
            raise CorpusError("private calibration observation is invalid")
    return raw, sha256_hex(body)


def _result_tuple(raw: dict[str, Any]) -> tuple[bool, int, int, int, int]:
    return (
        bool(raw["build_passed"]),
        int(raw["visible_tests_passed"]),
        int(raw["visible_tests_total"]),
        int(raw["hidden_tests_passed"]),
        int(raw["hidden_tests_total"]),
    )


def _canonical_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size < 1
        or path.stat().st_size > _MAX_OBSERVATION_BYTES
    ):
        raise CorpusError(f"{label} is invalid")
    try:
        body = path.read_bytes()
        raw: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"{label} is invalid") from error
    if not isinstance(raw, dict) or canonical_json_bytes(raw) != body:
        raise CorpusError(f"{label} is invalid")
    return raw, body


def _digest_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == _REPLICATES
        and value == sorted(value)
        and len(set(value)) == _REPLICATES
        and all(_sha256(item) for item in value)
    )


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "PRIVATE_CALIBRATION_OBSERVATION_SCHEMA",
    "PRIVATE_CALIBRATION_SCHEMA",
    "compile_private_calibration",
    "load_private_calibration",
]
