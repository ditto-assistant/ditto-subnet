"""The committed vectors match the code, and hold up to independent derivation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ditto_edge_protocol import vectors

VECTORS = Path(__file__).resolve().parents[1] / "testdata" / "conformance_v1.json"


def _load() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_committed_vectors_are_current() -> None:
    assert VECTORS.read_text(encoding="utf-8") == vectors.render(), (
        "regenerate with: python -m ditto_edge_protocol.vectors --write "
        "testdata/conformance_v1.json"
    )


def test_check_mode_reports_staleness(tmp_path: Path) -> None:
    stale = tmp_path / "v.json"
    stale.write_text("{}\n")
    assert vectors.main(["--check", str(stale)]) == 1
    assert vectors.main(["--check", str(VECTORS)]) == 0


def test_keys_and_signatures_rederive_from_the_raw_primitives() -> None:
    data = _load()
    for row in data["keys"]:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(row["seed_hex"]))
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert public.hex() == row["public_key_hex"]
        assert row["node_id"] == "edn_" + hashlib.sha256(public).hexdigest()[:32]
    for row in data["requests"]:
        body = row["body_utf8"].encode()
        canonical = "\n".join(
            [
                "ditto-edge-request:v1",
                row["method"],
                row["path"],
                str(row["timestamp"]),
                row["nonce_hex"],
                hashlib.sha256(body).hexdigest(),
            ]
        )
        assert canonical == row["canonical_utf8"]
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(row["seed_hex"]))
        assert key.sign(canonical.encode()).hex() == row["signature_hex"]


def test_possession_vectors_rederive_from_their_generator() -> None:
    for row in _load()["possession"]:
        blocks = bytearray()
        counter = 0
        while len(blocks) < row["size"]:
            blocks.extend(hashlib.sha256(f"{row['label']}:{counter}".encode()).digest())
            counter += 1
        data = bytes(blocks[: row["size"]])
        assert hashlib.sha256(data).hexdigest() == row["data_sha256"]
        window = data[row["offset"] : row["offset"] + row["length"]]
        proof = hashlib.sha256(bytes.fromhex(row["nonce_hex"]) + window).hexdigest()
        assert proof == row["proof_hex"]


def test_ledger_vectors_cover_every_ineligibility_reason() -> None:
    reasons = {
        reason for row in _load()["ledger"] for reason in row["expected"]["reasons"]
    }
    assert {
        "availability_below_floor",
        "revocation_violation",
        "integrity_failure",
    } <= reasons
    shares = [row["expected_hypothetical_share"] for row in _load()["ledger"]]
    assert sum(shares) == 1.0
