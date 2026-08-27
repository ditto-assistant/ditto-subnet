#!/usr/bin/env python3
"""Generate one production hotkey and stream its mnemonic into Secret Manager.

The mnemonic is never written to disk or printed. The public SS58 address and
the new Secret Manager version name are the only outputs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="ditto-app-dev")
    parser.add_argument("--secret", default="validator-prod-hotkey-mnemonic")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm must be exactly: {CONFIRMATION}")

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
    versions = _run_gcloud(
        [
            "secrets",
            "versions",
            "list",
            args.secret,
            "--project",
            args.project,
            "--format=value(name)",
        ]
    )
    if versions:
        raise RuntimeError(
            "refusing to generate a replacement: the target secret already has "
            "a version"
        )

    mnemonic = bt.Keypair.generate_mnemonic(n_words=24)
    keypair = bt.Keypair.create_from_mnemonic(mnemonic)
    version = _run_gcloud(
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

    print(f"validator_hotkey={keypair.ss58_address}")
    print(f"secret_version={version}")
    print("The mnemonic was streamed directly to Secret Manager and was not printed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
