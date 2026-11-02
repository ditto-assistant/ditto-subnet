"""Private credential handling for the actual supervised observer entry point."""

import os
from pathlib import Path

import pytest

from ditto.treasury.activity_observer import observer_token


def private_file(tmp_path, value=b"approved-test-token\n"):
    path = tmp_path / "credential"
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def test_private_credential_and_existing_environment_binding(tmp_path):
    assert observer_token(private_file(tmp_path)) == "approved-test-token"
    assert observer_token(None, environment_token="approved-test-token") == (
        "approved-test-token"
    )


@pytest.mark.parametrize(
    "value", [b"", b"x" * 8193, b"two\nlines", b"x\n\n", b"a b", b"\xff"]
)
def test_empty_oversized_and_malformed_files_refuse(tmp_path, value):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        observer_token(private_file(tmp_path, value))


def test_no_file_error_or_permission_fallback(tmp_path):
    path = private_file(tmp_path)
    path.chmod(0o640)
    with pytest.raises(ValueError, match="private file"):
        observer_token(path)
    with pytest.raises(ValueError, match="ambiguous"):
        observer_token(path, environment_token="another-test-token")
    with pytest.raises(FileNotFoundError):
        observer_token(tmp_path / "missing")


def test_wrong_owner_refuses(tmp_path, monkeypatch):
    path = private_file(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 99999)
    with pytest.raises(ValueError, match="private file"):
        observer_token(path)


def test_symlink_fifo_and_directory_refuse_without_blocking(tmp_path):
    path = private_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        observer_token(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="private file"):
        observer_token(fifo)
    with pytest.raises(ValueError, match="private file"):
        observer_token(tmp_path)


@pytest.mark.parametrize("token", ["", "a b", "a\nb", "é", "x" * 8193])
def test_environment_has_same_content_bounds(token):
    with pytest.raises(ValueError):
        observer_token(None, environment_token=token)


def test_staged_unit_has_no_signer_or_automatic_semantic_restart():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "infra/systemd/sn118-treasury-activity-observer.service").read_text()
    assert "LoadCredential=backroom-observe:" in unit
    assert "--token-file ${CREDENTIALS_DIRECTORY}/backroom-observe" in unit
    assert "Restart=no" in unit
    assert "ConditionPathExists=/etc/sn118-treasury-observer/activation.env" in unit
    assert "--transfer-journal" not in unit
    assert "BACKROOM_ACTIVITY_OBSERVER_TOKEN=" not in unit
