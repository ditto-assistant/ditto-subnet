"""One-time signer-host key ceremony. Never run during CI or Terraform apply.

The mnemonic is generated in memory and passed to Secret Manager on stdin. The
only program output is the public address. Run only after operator review and
a temporary secret-version-adder grant to this host's service account.
"""

from __future__ import annotations

import argparse
import subprocess
from urllib.request import Request, urlopen

import bittensor as bt

HOST_NAME = "sn118-treasury-signer"
SECRET_ID = "sn118-treasury-signing-key"


def _instance_name() -> str:
    request = Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/name",
        headers={"Metadata-Flavor": "Google"},
    )
    with urlopen(request, timeout=3) as response:  # noqa: S310 - GCE IMDS only
        return response.read(128).decode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != "CREATE SN118 TREASURY KEY":
        parser.error("exact operator confirmation is required")
    if _instance_name() != HOST_NAME:
        raise RuntimeError("key ceremony must run on the isolated treasury host")
    versions = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "list",
            SECRET_ID,
            f"--project={args.project}",
            "--format=value(name)",
        ],
        capture_output=True,
        check=True,
    )
    if versions.stdout.strip():
        raise RuntimeError(
            "secret already has a version; rotation needs separate review"
        )
    mnemonic = bt.Keypair.generate_mnemonic(n_words=24)
    address = bt.Keypair.create_from_mnemonic(mnemonic).ss58_address
    subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "add",
            SECRET_ID,
            f"--project={args.project}",
            "--data-file=-",
        ],
        input=mnemonic.encode(),
        capture_output=True,
        check=True,
    )
    print(address)


if __name__ == "__main__":
    main()
