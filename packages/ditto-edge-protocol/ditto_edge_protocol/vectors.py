"""Conformance vectors every edge/v1 implementation must reproduce.

The Desktop helper (Node.js) and any future node read
``testdata/conformance_v1.json`` and must derive the same node ids, canonical
request bytes, Ed25519 signatures, proofs and ledger rows. Ed25519 is
deterministic, so a signature vector is exact.

    python -m ditto_edge_protocol.vectors --check testdata/conformance_v1.json
    python -m ditto_edge_protocol.vectors --write testdata/conformance_v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ditto_edge_protocol import paths
from ditto_edge_protocol.ledger import (
    HoldInterval,
    LedgerParams,
    Observation,
    hypothetical_shares,
    score_window,
)
from ditto_edge_protocol.limits import GIB, PROTOCOL
from ditto_edge_protocol.proofs import digest_of, possession_proof
from ditto_edge_protocol.signing import (
    DeviceKey,
    binding_message,
    canonical_request,
)

_GENERATOR = (
    "python -m ditto_edge_protocol.vectors --write testdata/conformance_v1.json"
)


def _seed(label: str) -> bytes:
    return hashlib.sha256(f"ditto-edge-vector:{label}".encode()).digest()


def _bytes(label: str, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(f"{label}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:size])


def _keys() -> list[dict[str, str]]:
    rows = []
    for index in range(3):
        key = DeviceKey.from_seed(_seed(f"key-{index}"))
        rows.append(
            {
                "seed_hex": key.seed().hex(),
                "public_key_hex": key.public_key_hex,
                "node_id": key.node_id,
            }
        )
    return rows


def _requests() -> list[dict[str, Any]]:
    key = DeviceKey.from_seed(_seed("key-0"))
    digest = digest_of(b"ditto edge vector object")
    cases: list[tuple[str, str, int, str, str]] = [
        (
            "POST",
            paths.ENROLL,
            1_763_000_000,
            "00" * 16,
            json.dumps(
                {
                    "protocol": PROTOCOL,
                    "public_key_hex": key.public_key_hex,
                    "quota_bytes": 2 * GIB,
                    "client": {"name": "conformance", "version": "1"},
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
        (
            "POST",
            paths.POLL,
            1_763_000_030,
            "0f" * 16,
            '{"free_bytes":1073741824,"protocol":"edge/v1"}',
        ),
        ("GET", paths.object_path(digest), 1_763_000_031, "a5" * 16, ""),
        (
            "POST",
            paths.answer_path("1f" * 16),
            1_763_000_040,
            "ff" * 16,
            '{"outcome":"refused","refusal":"revoked"}',
        ),
    ]
    rows = []
    for method, path, timestamp, nonce, body in cases:
        body_bytes = body.encode()
        canonical = canonical_request(
            method=method,
            path=path,
            timestamp=timestamp,
            nonce_hex=nonce,
            body=body_bytes,
        )
        signed = key.sign_request(
            method=method,
            path=path,
            body=body_bytes,
            timestamp=timestamp,
            nonce_hex=nonce,
        )
        rows.append(
            {
                "seed_hex": key.seed().hex(),
                "node_id": key.node_id,
                "method": method,
                "path": path,
                "timestamp": timestamp,
                "nonce_hex": nonce,
                "body_utf8": body,
                "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
                "canonical_utf8": canonical.decode("ascii"),
                "signature_hex": signed.signature_hex,
            }
        )
    return rows


def _possession() -> list[dict[str, Any]]:
    rows = []
    for label, size, offset, length in (
        ("small", 37, 0, 37),
        ("middle", 200_000, 70_000, 65_536),
        ("tail", 70_001, 4_465, 65_536),
    ):
        data = _bytes(label, size)
        nonce = _seed(f"nonce-{label}").hex()
        rows.append(
            {
                "label": label,
                "data_sha256": digest_of(data),
                "data_generator": f"sha256('{label}:'+str(counter)) blocks, truncated",
                "size": size,
                "nonce_hex": nonce,
                "offset": offset,
                "length": length,
                "proof_hex": possession_proof(
                    nonce_hex=nonce, data=data, offset=offset, length=length
                ),
            }
        )
    return rows


def _bindings() -> list[dict[str, Any]]:
    key = DeviceKey.from_seed(_seed("key-1"))
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    issued_at = 1_763_000_000
    return [
        {
            "node_id": key.node_id,
            "hotkey_ss58": hotkey,
            "issued_at": issued_at,
            "message_utf8": binding_message(
                node_id=key.node_id, hotkey_ss58=hotkey, issued_at=issued_at
            ).decode("ascii"),
        }
    ]


def _ledger() -> list[dict[str, Any]]:
    params = LedgerParams(min_challenges=3, availability_floor=0.8)
    start, end = 1_763_000_000.0, 1_763_003_600.0
    cases: list[tuple[str, list[Observation], list[HoldInterval]]] = [
        (
            "honest_full_window",
            [Observation("possession", "passed")] * 5
            + [Observation("retrieval", "passed")]
            + [Observation("revoked", "refused_ok")],
            [
                HoldInterval(GIB, start - 600, None),
                HoldInterval(GIB // 2, start + 1800, None),
            ],
        ),
        (
            "half_online",
            [Observation("possession", "passed")] * 3
            + [Observation("possession", "missed")] * 3,
            [HoldInterval(GIB, start, None)],
        ),
        (
            "lazy_refills",
            [Observation("possession", "refilled")] * 4
            + [Observation("possession", "passed")],
            [HoldInterval(GIB, start, None)],
        ),
        (
            "kept_revoked_content",
            [Observation("possession", "passed")] * 5
            + [Observation("revoked", "violation")],
            [HoldInterval(GIB, start, None)],
        ),
        (
            "tampered_bytes",
            [Observation("possession", "passed")] * 5
            + [Observation("retrieval", "failed")],
            [HoldInterval(GIB, start, end - 1800)],
        ),
    ]
    rows: list[dict[str, Any]] = []
    units: dict[str, float] = {}
    for name, observations, holds in cases:
        score = score_window(
            observations=observations,
            holds=holds,
            window_start=start,
            window_end=end,
            params=params,
        )
        units[name] = score.verified_units
        rows.append(
            {
                "name": name,
                "params": asdict(params),
                "window_start": start,
                "window_end": end,
                "observations": [asdict(o) for o in observations],
                "holds": [asdict(h) for h in holds],
                "expected": {**asdict(score), "reasons": list(score.reasons)},
            }
        )
    shares = hypothetical_shares(units)
    for row, (name, _, _) in zip(rows, cases, strict=True):
        row["expected_hypothetical_share"] = shares[name]
    return rows


def build_vectors() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "generated_by": _GENERATOR,
        "keys": _keys(),
        "requests": _requests(),
        "possession": _possession(),
        "bindings": _bindings(),
        "ledger": _ledger(),
    }


def render() -> str:
    return json.dumps(build_vectors(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", type=Path, help="fail if this file is stale")
    mode.add_argument("--write", type=Path, help="regenerate this file")
    args = parser.parse_args(argv)
    if args.write is not None:
        args.write.write_text(render(), encoding="utf-8")
        return 0
    current = args.check.read_text(encoding="utf-8")
    if current != render():
        print(f"{args.check} is stale; regenerate with: {_GENERATOR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
