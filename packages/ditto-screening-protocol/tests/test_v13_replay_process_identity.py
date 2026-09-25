"""Independent replay process proof cannot be retargeted across workers or APIs."""

from hashlib import sha256

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.v13_replay_process_identity import V13ReplayProcessProof


def _proof(**changes: object) -> V13ReplayProcessProof:
    fields: dict[str, object] = {
        "purpose": "claim",
        "node_id": "subnet-screener-2",
        "instance_id": "subnet-screener-2-worker-1",
        "path": "/screener/verification-replays/claim",
        "body_sha256": sha256(b"").hexdigest(),
        "issued_at": 1_790_200_000,
        "nonce": "a" * 32,
    }
    fields.update(changes)
    return V13ReplayProcessProof.model_validate(fields)


def test_canonical_bytes_are_stable_and_domain_separated() -> None:
    proof = _proof()
    assert proof.signing_bytes() == _proof().signing_bytes()
    assert proof.signing_bytes().startswith(b"ditto-v13-replay-process-proof:v1\n")


@pytest.mark.parametrize(
    "change",
    [
        {"instance_id": "subnet-screener-2-worker-2"},
        {"body_sha256": "b" * 64},
        {"issued_at": 1_790_200_001},
        {"nonce": "b" * 32},
    ],
)
def test_changed_claim_scope_changes_signed_bytes(change: dict[str, object]) -> None:
    assert _proof(**change).signing_bytes() != _proof().signing_bytes()


@pytest.mark.parametrize(
    "change",
    [
        {"node_id": "subnet-screener-1"},
        {"instance_id": "subnet-screener-1-worker-1"},
        {"instance_id": "subnet-screener-2-worker-0"},
        {"instance_id": "subnet-screener-2-worker-1-extra"},
        {"path": "/screener/agent/claim"},
        {"purpose": "heartbeat"},
        {"nonce": "not-random"},
        {"body_sha256": "A" * 64},
    ],
)
def test_mismatched_scope_is_rejected(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _proof(**change)


def test_heartbeat_has_a_distinct_api_scope() -> None:
    heartbeat = _proof(purpose="heartbeat", path="/screener/heartbeat")
    assert heartbeat.signing_bytes() != _proof().signing_bytes()


def test_claim_requires_current_time_and_exact_authenticated_process() -> None:
    proof = _proof()
    assert proof.is_fresh(now=1_790_200_000)
    assert proof.is_fresh(now=1_790_200_030)
    assert not proof.is_fresh(now=1_790_200_031)
    assert not proof.is_fresh(now=1_790_199_994)
    assert proof.matches_request(
        purpose="claim",
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-1",
        body_sha256=sha256(b"").hexdigest(),
    )
    assert not proof.matches_request(
        purpose="claim",
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-2",
        body_sha256=sha256(b"").hexdigest(),
    )
    assert not proof.matches_request(
        purpose="heartbeat",
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-1",
        body_sha256=sha256(b"").hexdigest(),
    )


@pytest.mark.parametrize(
    "purpose",
    ["renew", "inputs", "build-upload", "build-verify", "receipts", "finish"],
)
def test_lease_proof_is_bound_to_exact_route_and_method(purpose: str) -> None:
    replay_id = "123e4567-e89b-42d3-a456-426614174000"
    path = f"/screener/verification-replays/{replay_id}/{purpose}"
    method = "GET" if purpose == "inputs" else "POST"
    proof = _proof(purpose=purpose, path=path, method=method)
    assert proof.matches_request(
        purpose=purpose,
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-1",
        body_sha256=sha256(b"").hexdigest(),
        method=method,
        path=path,
    )
    assert not proof.matches_request(
        purpose=purpose,
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-1",
        body_sha256=sha256(b"").hexdigest(),
        method=method,
        path=path.replace(replay_id, "123e4567-e89b-42d3-a456-426614174001"),
    )
    with pytest.raises(ValidationError):
        _proof(purpose=purpose, path=path, method="POST" if method == "GET" else "GET")
