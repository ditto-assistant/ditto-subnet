"""Wire contract round trips and strict validation of known fields."""

from __future__ import annotations

import json

import numpy as np
import pytest

from query_adapter_pilot.contract import (
    CONTRACT_VERSION,
    DIM,
    HTTP_STATUS,
    OUTCOMES,
    AdapterRef,
    AdaptRequest,
    AdaptResponse,
    ContractError,
    ErrorCode,
    ProvisionRequest,
    encode_f32,
    error_body,
    make_request_id,
    parse_error,
)
from query_adapter_pilot.reference import apply, synthetic_adapter, synthetic_query
from query_adapter_pilot.workload import Workload


def _ref(seed: int = 1, tenant: str = "syn-t1", version: int = 1) -> AdapterRef:
    return AdapterRef(tenant, version, synthetic_adapter(seed).digest())


def _wire(obj: dict[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(obj))  # type: ignore[no-any-return]


def test_provision_round_trip_and_digest_binding() -> None:
    w = synthetic_adapter(1)
    req = ProvisionRequest(_ref(), w)
    back = ProvisionRequest.from_wire(_wire(req.to_wire()))
    assert back.ref == req.ref and back.weights.digest() == w.digest()
    bad = req.to_wire()
    bad["adapter_digest"] = synthetic_adapter(2).digest()
    with pytest.raises(ContractError) as e:
        ProvisionRequest.from_wire(_wire(bad))
    assert e.value.code == ErrorCode.DIGEST_MISMATCH


def test_adapt_round_trip_ignores_unknown_fields() -> None:
    req = AdaptRequest(make_request_id("run1", "syn-t1", 0), _ref(), synthetic_query(1))
    body = req.to_wire() | {"future_field": {"x": 1}}
    back = AdaptRequest.from_wire(_wire(body))
    assert back.request_id == req.request_id and np.array_equal(back.query, req.query)
    vec = apply(synthetic_adapter(1), req.query)
    resp = AdaptResponse(req.request_id, req.ref, vec, 12, "hit")
    assert np.array_equal(AdaptResponse.from_wire(_wire(resp.to_wire())).vector, vec)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda b: b.update(contract="query-adapter/v0"), ErrorCode.CONTRACT_MISMATCH),
        (lambda b: b.update(query=encode_f32(np.ones(DIM - 1))), ErrorCode.BAD_SHAPE),
        (lambda b: b.update(query=encode_f32(np.full(DIM, np.nan))), ErrorCode.NON_FINITE),
        (lambda b: b.update(query=encode_f32(np.full(DIM, 0.5))), ErrorCode.BAD_NORM),
        (lambda b: b.update(tenant_id="user-42"), ErrorCode.INVALID_REQUEST),
        (lambda b: b.update(adapter_version=0), ErrorCode.INVALID_REQUEST),
        (lambda b: b.update(adapter_version=True), ErrorCode.INVALID_REQUEST),
        (lambda b: b.update(request_id="free-form"), ErrorCode.INVALID_REQUEST),
        (lambda b: b.update(query="not base64!"), ErrorCode.INVALID_REQUEST),
    ],
)
def test_adapt_request_rejects(mutate: object, code: str) -> None:
    body = AdaptRequest(make_request_id("r", "syn-t1", 1), _ref(), synthetic_query(1)).to_wire()
    mutate(body)  # type: ignore[operator]
    with pytest.raises(ContractError) as e:
        AdaptRequest.from_wire(_wire(body))
    assert e.value.code == code


def test_error_bodies_and_vocabulary() -> None:
    assert parse_error(error_body(ErrorCode.STALE_VERSION, "x")) == (ErrorCode.STALE_VERSION, "x")
    assert parse_error({"error": {"code": "MADE_UP"}})[0] == ErrorCode.INVALID_REQUEST
    assert parse_error("nope")[0] == ErrorCode.INVALID_REQUEST
    assert set(HTTP_STATUS) >= {ErrorCode.MISSING_VERSION, ErrorCode.STALE_VERSION}
    assert "ok" in OUTCOMES and "duplicate" in OUTCOMES
    assert CONTRACT_VERSION == "query-adapter/v1"
    with pytest.raises(ContractError):
        make_request_id("Run_1", "syn-t1", 0)


def test_workload_parses_replays_and_divergence() -> None:
    raw = {
        "contract": CONTRACT_VERSION,
        "kind": "synthetic-workload",
        "name": "unit",
        "users": [
            {
                "user_ref": "syn-u1",
                "adapters": [{"version": 1, "seed": 11}, {"version": 2, "seed": 12}],
                "provision": [1, 2],
                "authorized_version": 1,
            }
        ],
        "requests": [{"user_ref": "syn-u1", "query_seed": 1}, {"replay_of": 0}],
    }
    wl = Workload.from_json(raw)
    assert wl.requests[1].replay_of == 0 and wl.requests[1].query_seed == 1
    assert wl.user("syn-u1").adapter(2) is not None
    raw["users"][0]["user_ref"] = "real-user"  # type: ignore[index]
    with pytest.raises(ContractError):
        Workload.from_json(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w.update(users=5),
        lambda w: w["users"][0].update(adapters={"version": 1}),
        lambda w: w["users"][0].update(provision="1"),
        lambda w: w.update(requests={"user_ref": "syn-u1"}),
        lambda w: w["requests"][0].update(user_ref=["syn-u1"]),
        lambda w: w["users"][0]["adapters"][0].update(alpha=0.0),
        lambda w: w["users"][0]["adapters"][0].update(alpha=1.5),
    ],
)
def test_workload_rejects_malformed_fields_as_contract_errors(mutate: object) -> None:
    raw: dict[str, object] = {
        "contract": CONTRACT_VERSION,
        "kind": "synthetic-workload",
        "name": "unit",
        "users": [
            {
                "user_ref": "syn-u1",
                "adapters": [{"version": 1, "seed": 11}],
                "provision": [1],
                "authorized_version": 1,
            }
        ],
        "requests": [{"user_ref": "syn-u1", "query_seed": 1}],
    }
    Workload.from_json(json.loads(json.dumps(raw)))
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(ContractError):
        Workload.from_json(raw)
