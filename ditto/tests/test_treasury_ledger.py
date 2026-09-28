import json

import pytest

from ditto.treasury.ledger import append, verify


def test_rehearsal_chain_detects_tampering(tmp_path) -> None:
    path = tmp_path / "treasury.jsonl"
    first = append(path, event="quote", payload={"block": 10})
    second = append(path, event="dry_run", payload={"route": "tao"})
    assert first["hash"] == second["previous_hash"]
    assert verify(path) == (2, second["hash"])
    assert path.stat().st_mode & 0o777 == 0o600

    rows = path.read_text().splitlines()
    broken = json.loads(rows[0])
    broken["payload"]["block"] = 11
    path.write_text(json.dumps(broken) + "\n" + rows[1] + "\n")
    with pytest.raises(ValueError, match="breaks"):
        verify(path)
    with pytest.raises(ValueError, match="read-only"):
        append(path, event="payment_settled", payload={})
