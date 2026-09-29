"""Archive budgets and pair classification cannot establish source authority."""

import gzip
import io
import tarfile

import pytest
from pydantic import ValidationError

from ditto.api_models.submission_attempts import AttemptObservationPolicy
from ditto.api_server import submission_attempts as attempts
from ditto.tests.submission_attempt_fixtures import archive, source


def compare(current, prior, infra=False):
    return attempts.classify_pair(
        attempts.artifact_profile(current),
        attempts.artifact_profile(prior),
        infrastructure_failure=infra,
    )[0]


def test_same_runtime_survives_rename_and_repacking():
    assert (
        compare(
            archive({"renamed.py": source("memory")}),
            archive({"main.py": source("memory")}),
        )
        == "small_source_delta"
    )


@pytest.mark.parametrize(
    "infra,expected",
    [
        (False, "packaging_only_repair"),
        (True, "infrastructure_retry"),
    ],
)
def test_runtime_preserving_repair_and_infrastructure(infra, expected):
    prior = archive({"main.py": source("memory"), "Dockerfile": b"FROM a"})
    current = archive({"main.py": source("memory"), "Dockerfile": b"FROM b"})
    assert compare(current, prior, infra) == expected


def test_material_residual_change():
    assert (
        compare(
            archive({"main.py": source("planning")}),
            archive({"main.py": source("memory")}),
        )
        == "material_new_work"
    )


def test_changed_binary_is_inconclusive():
    prior = archive({"main.py": source("memory"), "asset.bin": b"\x00old"})
    current = archive({"main.py": source("planning"), "asset.bin": b"\x00new"})
    assert compare(current, prior) == "inconclusive"


def test_small_edit_residual_overlap_is_observed():
    prior = source("memory") * 10
    current = prior + b"\n# revised comment\n"
    assert (
        compare(archive({"main.py": current}), archive({"main.py": prior}))
        == "small_source_delta"
    )


@pytest.mark.parametrize(
    "path", ["/absolute.py", "../escape.py", "a/../b.py", "a\\b.py"]
)
def test_unsafe_paths_are_unavailable(path):
    with pytest.raises(attempts.ProfileUnavailable, match="unsafe member"):
        attempts.artifact_profile(archive({path: source("memory")}))


def test_member_and_compressed_budgets(monkeypatch):
    monkeypatch.setattr(attempts, "MAX_MEMBERS", 1)
    with pytest.raises(attempts.ProfileUnavailable, match="member budget"):
        attempts.artifact_profile(archive({"a.py": b"a", "b.py": b"b"}))
    with pytest.raises(attempts.ProfileUnavailable, match="archive byte budget"):
        attempts.artifact_profile(b"x" * (attempts.MAX_ARCHIVE_BYTES + 1))


def test_gzip_expansion_in_trailing_padding_is_bounded(monkeypatch):
    data = archive({"main.py": source("memory")})
    unpacked = gzip.decompress(data)
    monkeypatch.setattr(attempts, "MAX_UNPACKED_BYTES", len(unpacked) + 1024)
    with pytest.raises(attempts.ProfileUnavailable, match="decompressed byte budget"):
        attempts.artifact_profile(gzip.compress(unpacked + b"\x00" * 4096))


def test_duplicate_normalized_paths_and_symlinks_are_unavailable():
    with pytest.raises(attempts.ProfileUnavailable, match="invalid member"):
        attempts.artifact_profile(archive({"src//main.py": b"a", "src/main.py": b"b"}))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        member = tarfile.TarInfo("main.py")
        member.type = tarfile.SYMTYPE
        member.linkname = "/tmp/unsafe"
        packed.addfile(member)
    with pytest.raises(attempts.ProfileUnavailable, match="unsafe member"):
        attempts.artifact_profile(buffer.getvalue())


@pytest.mark.parametrize(
    "data",
    [b"garbage", b"", archive({"Dockerfile": b"FROM a"})],
    ids=["invalid", "empty", "packaging-only"],
)
def test_unreadable_or_empty_runtime_is_inconclusive(data):
    with pytest.raises(attempts.ProfileUnavailable):
        attempts.artifact_profile(data)


def test_policy_records_provenance_and_cannot_grant_clearance(monkeypatch):
    policy = attempts.observation_policy("exact-build")
    assert policy.source_build == "exact-build"
    assert policy.report_only and policy.admission_effect == "none"
    assert not policy.source_clearance and not policy.integrity_clearance
    data = policy.model_dump()
    assert (
        AttemptObservationPolicy.model_validate({**data, "future": "ignored"}) == policy
    )
    for key, value in [
        ("report_only", False),
        ("source_clearance", True),
        ("integrity_clearance", True),
        ("admission_effect", "enforce"),
    ]:
        with pytest.raises(ValidationError):
            AttemptObservationPolicy.model_validate({**data, key: value})
    monkeypatch.setattr(attempts, "SMALL_DELTA_JACCARD", 0.99)
    assert (
        attempts.observation_policy("exact-build").settings_digest
        != policy.settings_digest
    )


def test_standard_root_directory_is_supported():
    data = archive({"main.py": source("memory")})
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        directory = tarfile.TarInfo(".")
        directory.type = tarfile.DIRTYPE
        packed.addfile(directory)
        member = tarfile.TarInfo("./main.py")
        member.size = len(source("memory"))
        packed.addfile(member, io.BytesIO(source("memory")))
    assert compare(buffer.getvalue(), data) == "small_source_delta"
