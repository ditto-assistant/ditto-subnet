#!/usr/bin/env python3
"""Generate one production hotkey and stream its mnemonic into Secret Manager.

The mnemonic is never written to disk or printed. The public SS58 address and
the new numeric Secret Manager version ID are the only outputs.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

import bittensor as bt

CONFIRMATION = "CREATE GCP VALIDATOR HOTKEY"


def _run_gcloud(args: list[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["gcloud", *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        # gcloud does not echo --data-file=- input. Keep the exception generic
        # anyway so future CLI changes cannot accidentally include key bytes.
        raise RuntimeError(f"gcloud command failed with exit code {result.returncode}")
    return result.stdout.strip()


def _write_result(path: Path, *, hotkey: str, version: str) -> None:
    """Atomically persist public recovery metadata, never signing material."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"validator_hotkey={hotkey}\n")
            handle.write(f"secret_version={version}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        pending.unlink(missing_ok=True)


def _parse_result(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"validator_hotkey", "secret_version"}:
            fields[key] = value
    return fields


def _print_result(*, hotkey: str, version: str) -> None:
    print(f"validator_hotkey={hotkey}")
    print(f"secret_version={version}")
    print("The mnemonic was streamed directly to Secret Manager and was not printed.")


def _numeric_version(value: str) -> str:
    """Return a fail-closed numeric Secret Manager version identifier."""
    match = re.fullmatch(r"(?:.*/versions/)?([1-9][0-9]*)", value.strip())
    if not match:
        raise RuntimeError(f"unexpected Secret Manager version name: {value!r}")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="ditto-app-dev")
    parser.add_argument("--secret", default="validator-prod-hotkey-mnemonic")
    parser.add_argument("--result-file", type=Path)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path("/tmp/validator-hotkey-generator.lock"),
    )
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm must be exactly: {CONFIRMATION}")

    args.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.lock_file.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another hotkey generation is already running") from exc

        _run_gcloud(
            [
                "secrets",
                "describe",
                args.secret,
                "--project",
                args.project,
                "--format=value(name)",
            ]
        )
        version_names = _run_gcloud(
            [
                "secrets",
                "versions",
                "list",
                args.secret,
                "--project",
                args.project,
                "--format=value(name)",
            ]
        ).splitlines()
        versions = [_numeric_version(version) for version in version_names]
        result = _parse_result(args.result_file) if args.result_file else {}

        # A disconnected SSH client does not terminate the generator. If the
        # process or VM did fail after adding the only version but before
        # recording its name, recover the non-secret result without revealing
        # or re-reading the mnemonic.
        if result.get("validator_hotkey") and result.get("secret_version") == "pending":
            if len(versions) == 1:
                _write_result(
                    args.result_file,
                    hotkey=result["validator_hotkey"],
                    version=versions[0],
                )
                _print_result(hotkey=result["validator_hotkey"], version=versions[0])
                return 0
            if not versions:
                args.result_file.unlink()

        if versions:
            if (
                len(versions) == 1
                and result.get("validator_hotkey")
                and result.get("secret_version") == versions[0]
            ):
                _print_result(hotkey=result["validator_hotkey"], version=versions[0])
                return 0
            raise RuntimeError(
                "refusing to generate a replacement: the target secret already has "
                "a version"
            )

        mnemonic = bt.Keypair.generate_mnemonic(n_words=24)
        keypair = bt.Keypair.create_from_mnemonic(mnemonic)
        if args.result_file:
            _write_result(
                args.result_file,
                hotkey=keypair.ss58_address,
                version="pending",
            )
        version = _numeric_version(
            _run_gcloud(
                [
                    "secrets",
                    "versions",
                    "add",
                    args.secret,
                    "--project",
                    args.project,
                    "--data-file=-",
                    "--format=value(name)",
                ],
                stdin=f"{mnemonic}\n",
            )
        )
        if args.result_file:
            _write_result(
                args.result_file,
                hotkey=keypair.ss58_address,
                version=version,
            )
        _print_result(hotkey=keypair.ss58_address, version=version)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
