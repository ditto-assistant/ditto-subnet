"""Tests for deterministic validator build identification."""

from pathlib import Path

from ditto import __version__
from ditto.validator.build_info import source_digest, validator_build_info


def test_source_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("b = 2\n")
    (tmp_path / "a.py").write_text("a = 1\n")
    first = source_digest(tmp_path)
    assert first == source_digest(tmp_path)
    (tmp_path / "a.py").write_text("a = 3\n")
    assert source_digest(tmp_path) != first


def test_build_info_has_public_protocol_identity() -> None:
    info = validator_build_info()
    assert info.software_version == __version__
    assert info.protocol_version == 10
    assert len(info.code_digest) == 64
