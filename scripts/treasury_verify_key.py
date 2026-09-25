"""Re-derive the public address from pinned Secret Manager version 1 on signer.

Only the public address is printed. Keep the expected address from the initial
ceremony in a reviewed, non-secret record and compare it on the signer host.
"""

from __future__ import annotations

import argparse
import subprocess

import bittensor as bt
from treasury_create_key import HOST_NAME, SECRET_ID, _instance_name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--expected-address", required=True)
    args = parser.parse_args()
    if _instance_name() != HOST_NAME:
        raise RuntimeError("key verification must run on the isolated treasury host")
    result = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "1",
            f"--secret={SECRET_ID}",
            f"--project={args.project}",
        ],
        capture_output=True,
        check=True,
    )
    address = bt.Keypair.create_from_mnemonic(result.stdout.decode()).ss58_address
    if address != args.expected_address:
        raise RuntimeError("stored key does not match the reviewed public address")
    print(address)


if __name__ == "__main__":
    main()
