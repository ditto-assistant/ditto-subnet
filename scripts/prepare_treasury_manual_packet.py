#!/usr/bin/env python3
"""Prepare public-only offline manual-range authority. Never loads a wallet."""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from ditto.treasury.collector_chain import load_policy
from ditto_screening_protocol.treasury_manual_limit import ManualLimitPolicy


def prepare(collector_approval: Path, expected_digest: str, output: Path):
    policy = load_policy(collector_approval, expected_digest)
    if not policy.enabled:
        raise ValueError("enabled exact signed custody policy required")
    limit = ManualLimitPolicy(
        collector_policy_digest=policy.digest,
        collector_coldkey=policy.collector_coldkey,
        revision=1,
        max_amount_rao=2_500_000_000_000,
    )
    for parent in (output, *output.parents):
        if parent.is_symlink():
            raise ValueError("symlink output refused")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "public").mkdir(mode=0o700)
    # Rewrite only the public known-field policy and its existing signature.
    previous = json.loads(collector_approval.read_text())
    files = {
        "manual-limit-policy.json": json.dumps(limit.model_dump(), indent=2) + "\n",
        "collector-approval.json": json.dumps(
            {"policy": asdict(policy), "signature": previous["signature"]}, indent=2
        )
        + "\n",
    }
    config = {
        "policy": limit.model_dump(),
        "digest": limit.digest,
        "input_hashes": {
            name: hashlib.sha256(data.encode()).hexdigest()
            for name, data in files.items()
        },
    }
    template = Path(__file__).with_name("offline_sign_treasury_manual.py").read_text()
    marker = "CONFIG = None  # PACKET_CONFIGURATION"
    if template.count(marker) != 1:
        raise ValueError("offline template configuration marker changed")
    files["sign-manual-limit.py"] = template.replace(marker, "CONFIG = " + repr(config))
    files["sign.sh"] = """#!/bin/bash
gamma_manual_sign() {
    local gamma_packet gamma_rc
    gamma_packet=$(dirname -- "${BASH_SOURCE[0]}") || return 1
    gamma_packet=$(CDPATH= cd -- "$gamma_packet" && pwd -P) || return 1
    cd "$HOME" || return 1
    if ! (cd "$gamma_packet" && sha256sum -c SHA256SUMS); then
        printf '%s\\n' 'packet-integrity-check-failed; no signing performed' \\
            > "$gamma_packet/public/launcher-error.txt"
        return 1
    fi
    python3 -I "$gamma_packet/sign-manual-limit.py"
    gamma_rc=$?
    if [ "$gamma_rc" -ne 0 ]; then
        printf 'python-exit-code=%s; see signing-result.json\\n' "$gamma_rc" \\
            > "$gamma_packet/public/launcher-error.txt"
    fi
    return "$gamma_rc"
}
gamma_manual_sign
"""
    files["START-HERE.txt"] = (
        "Approve manual transfers up to 2,500 SN118 alpha. Run bash sign.sh on the "
        "existing offline Ubuntu laptop. Type SIGN GAMMA MANUAL 2500, then unlock "
        "the existing encrypted collector coldkey. No new wallet, seed copying, "
        "transaction or journal migration. Gamma allocation and recurring "
        "authority unchanged. Only proved earnings may be used. Public approval "
        "and sanitized status are saved under public/. Eject and return USB.\n"
        f"Approval digest: {limit.digest}\n"
    )
    for name, data in files.items():
        (output / name).write_text(data)
    (output / "SHA256SUMS").write_text(
        "".join(
            hashlib.sha256(data.encode()).hexdigest() + "  " + name + "\n"
            for name, data in sorted(files.items())
        )
    )
    return {
        "approval_digest": limit.digest,
        "output": str(output),
        "max_manual_alpha": "2500",
        "signature_required": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector-approval", type=Path, required=True)
    parser.add_argument("--collector-policy-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.collector_approval, args.collector_policy_digest, args.output)
        )
    )


if __name__ == "__main__":
    main()
