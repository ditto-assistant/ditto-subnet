#!/usr/bin/env python3
"""sr25519 signer for the model-relay /api/v1/inference/exchange handshake.

Run inside the platform venv (bittensor available):
  cd apps/platform && uv run python ../../localstack/phase1/exchange_sign.py <cmd> ...

Commands:
  addr                          print the validator hotkey (ss58) for the fixed URI
  exchange <grant_id> <bpk>     print the exchange request JSON (signed) for grant_id
                                and broker_public_key (base64url, no padding)

The signed message mirrors model-relay exchangeMessage (crypto.go):
  validator-inference:v1:{hotkey}:{grant_id}:{bpk_no_pad}:{nonce}:{requested_at}
requested_at is isoformatMicro:  YYYY-MM-DDTHH:MM:SS.ffffff+00:00
"""

import json
import sys
import uuid
from datetime import UTC, datetime

import bittensor

URI = "//DittoLocalValidator"


def keypair() -> "bittensor.Keypair":
    return bittensor.Keypair.create_from_uri(URI)


def iso_micro(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "addr"
    kp = keypair()
    if cmd == "addr":
        print(kp.ss58_address)
        return
    if cmd == "exchange":
        grant_id = sys.argv[2]
        bpk = sys.argv[3].rstrip("=")  # broker_public_key sans padding
        nonce = str(uuid.uuid4())
        requested_at = iso_micro(datetime.now(UTC))
        message = (
            f"validator-inference:v1:{kp.ss58_address}:{grant_id}"
            f":{bpk}:{nonce}:{requested_at}"
        )
        sig = kp.sign(message.encode())
        print(
            json.dumps(
                {
                    "validator_hotkey": kp.ss58_address,
                    "grant_id": grant_id,
                    "broker_public_key": bpk,
                    "nonce": nonce,
                    "requested_at": requested_at,
                    "signature": sig.hex(),
                }
            )
        )
        return
    sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
