"""Cross-language vectors for the shadow coding inference contract."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from ditto.api_models.coding_evaluation import CodingAuthoringModelEvidence
from ditto.api_models.coding_inference import (
    MAX_CANONICAL_INFERENCE_BYTES,
    CodingInferenceLockedRequest,
    CodingInferenceModelEvidence,
    CodingInferenceNormalizedResponse,
    CodingInferencePolicy,
    CodingInferenceProviderResponse,
    CodingInferenceProviderSettlement,
    CodingInferenceReceiptBinding,
    CodingInferenceReceiptSet,
    CodingInferenceSystemPrompt,
    CodingInferenceToolSchema,
    coding_inference_canonical_json_bytes,
    derive_model_evidence,
    effective_inference_request_budget,
    locked_request_digest,
    model_evidence_digest,
    normalize_provider_response,
    normalized_response_digest,
    parse_coding_inference_json,
    policy_digest,
    provider_settlement_digest,
    receipt_set_digest,
    system_prompt_digest,
    tool_schema_digest,
)

_ROOT = Path(__file__).parents[5]
_TESTDATA = _ROOT / "packages/dittobench-coding-contract/testdata"
_MINER_VECTOR = _TESTDATA / "coding_inference_miner_v1.json"
_POLICY_VECTOR = _TESTDATA / "coding_inference_policy_v1.json"
_GENERATOR = _ROOT / "packages/dittobench-coding-contract/generate_inference_vectors.py"


def test_validator_inference_contract_module_matches_platform_byte_for_byte() -> None:
    validator = _ROOT / "ditto/api_models/coding_inference.py"
    platform = _ROOT / "apps/platform/ditto/api_models/coding_inference.py"
    assert platform.read_bytes() == validator.read_bytes()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _body(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _parse(model: type, value: object):
    return parse_coding_inference_json(model, _body(value))


def _receipt_binding(
    receipts: CodingInferenceReceiptSet,
) -> CodingInferenceReceiptBinding:
    return CodingInferenceReceiptBinding(
        ticket_id=receipts.ticket_id,
        case_id=receipts.case_id,
        profile_capability_id=receipts.profile_capability_id,
        grant_id=receipts.grant_id,
        generation=receipts.generation,
        inference_grant_sha256=receipts.inference_grant_sha256,
        request_budget=receipts.request_budget,
        prompt_token_budget=receipts.prompt_token_budget,
        completion_token_budget=receipts.completion_token_budget,
    )


def _settlements(
    vector: dict,
    name: str,
    policy: CodingInferencePolicy,
) -> list[CodingInferenceProviderSettlement]:
    values = [
        _parse(CodingInferenceProviderSettlement, value)
        for value in vector["provider_settlements"][name]
    ]
    assert [provider_settlement_digest(value, policy) for value in values] == vector[
        "expected"
    ]["provider_settlement_sha256"][name]
    return values


def test_generated_inference_vectors_are_current() -> None:
    completed = subprocess.run(
        [sys.executable, str(_GENERATOR), "--check"],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_miner_and_policy_vectors_share_exact_prompt_and_tools() -> None:
    miner = _load(_MINER_VECTOR)
    vector = _load(_POLICY_VECTOR)
    prompt = _parse(CodingInferenceSystemPrompt, miner["system_prompt"])
    tools = _parse(CodingInferenceToolSchema, miner["tool_schema"])
    policy = _parse(CodingInferencePolicy, vector["policy"])

    assert system_prompt_digest(prompt) == miner["expected"]["prompt_sha256"]
    assert tool_schema_digest(tools) == miner["expected"]["tool_schema_sha256"]
    assert policy.prompt_sha256 == miner["expected"]["prompt_sha256"]
    assert policy.tool_schema_sha256 == miner["expected"]["tool_schema_sha256"]
    assert policy_digest(policy) == vector["expected"]["inference_grant_sha256"]

    observed_requests = []
    for index, raw in enumerate(vector["locked_requests"]):
        request = _parse(CodingInferenceLockedRequest, raw)
        observed_requests.append(locked_request_digest(request, policy))
        assert request.tools == tools.tools
        assert [
            message.model_dump(mode="json") for message in request.messages
        ] == miner["turns"][index]["messages"]
    assert observed_requests == vector["expected"]["locked_request_sha256"]


def test_normalized_responses_receipts_and_evidence_match_vectors() -> None:
    vector = _load(_POLICY_VECTOR)
    policy = _parse(CodingInferencePolicy, vector["policy"])
    response_digests = []
    for raw, expected_raw in zip(
        vector["provider_responses"],
        vector["normalized_provider_responses"],
        strict=True,
    ):
        normalized = normalize_provider_response(
            _parse(CodingInferenceProviderResponse, raw), policy
        )
        expected = _parse(CodingInferenceNormalizedResponse, expected_raw)
        assert normalized == expected
        response_digests.append(normalized_response_digest(normalized, policy))
    assert response_digests == vector["expected"]["normalized_response_sha256"]
    invalid_projection = vector["invalid_provider_response_projections"][
        "response_invalid"
    ]
    assert (
        hashlib.sha256(
            coding_inference_canonical_json_bytes(invalid_projection)
        ).hexdigest()
        == vector["expected"]["invalid_provider_response_sha256"]["response_invalid"]
    )

    omitted_calls = copy.deepcopy(vector["provider_responses"][1])
    omitted_calls["choices"][0]["message"].pop("tool_calls")
    normalized_omission = normalize_provider_response(
        _parse(CodingInferenceProviderResponse, omitted_calls), policy
    )
    assert normalized_omission.choices[0].message.tool_calls == []

    multiple_calls = copy.deepcopy(vector["provider_responses"][0])
    second_call = copy.deepcopy(
        multiple_calls["choices"][0]["message"]["tool_calls"][0]
    )
    second_call["id"] = "call-second-forbidden"
    multiple_calls["choices"][0]["message"]["tool_calls"].append(second_call)
    with pytest.raises(ValueError):
        _parse(CodingInferenceProviderResponse, multiple_calls)

    authority = _parse(CodingInferenceReceiptSet, vector["receipt_sets"]["complete"])
    binding = _receipt_binding(authority)
    not_invoked = derive_model_evidence(policy, binding, None)
    CodingAuthoringModelEvidence.model_validate_json(not_invoked.model_dump_json())
    assert not_invoked == _parse(
        CodingInferenceModelEvidence, vector["model_evidence"]["not_invoked"]
    )
    assert (
        model_evidence_digest(not_invoked, policy)
        == vector["expected"]["model_evidence_sha256"]["not_invoked"]
    )

    for name in ("complete", "retry_complete", "provider_failure", "response_invalid"):
        receipts = _parse(CodingInferenceReceiptSet, vector["receipt_sets"][name])
        settlements = _settlements(vector, name, policy)
        assert receipts.request_budget == effective_inference_request_budget(
            vector["task_budgets"]["workspace_tool_calls"]
        )
        assert (
            receipts.prompt_token_budget == vector["task_budgets"]["model_input_tokens"]
        )
        assert (
            receipts.completion_token_budget
            == vector["task_budgets"]["model_output_tokens"]
        )
        expected_evidence = _parse(
            CodingInferenceModelEvidence, vector["model_evidence"][name]
        )
        assert (
            receipt_set_digest(receipts, policy)
            == vector["expected"]["provider_receipt_set_sha256"][name]
        )
        derived = derive_model_evidence(policy, binding, receipts, settlements)
        assert derived == expected_evidence
        CodingAuthoringModelEvidence.model_validate_json(derived.model_dump_json())
        assert (
            model_evidence_digest(derived, policy)
            == vector["expected"]["model_evidence_sha256"][name]
        )

    for field, value in (
        ("ticket_id", UUID("77777777-7777-4777-8777-777777777777")),
        ("case_id", "case-other"),
        ("profile_capability_id", "profile-other"),
        ("grant_id", UUID("88888888-8888-4888-8888-888888888888")),
        ("generation", binding.generation + 1),
        ("inference_grant_sha256", "f" * 64),
        ("request_budget", binding.request_budget - 1),
        ("prompt_token_budget", binding.prompt_token_budget - 1),
        ("completion_token_budget", binding.completion_token_budget - 1),
    ):
        drifted = binding.model_copy(update={field: value})
        with pytest.raises(ValueError):
            derive_model_evidence(
                policy, drifted, authority, _settlements(vector, "complete", policy)
            )

    complete_settlements = _settlements(vector, "complete", policy)
    with pytest.raises(ValueError):
        derive_model_evidence(policy, binding, authority, [])
    with pytest.raises(ValueError):
        derive_model_evidence(
            policy, binding, authority, list(reversed(complete_settlements))
        )
    tampered = list(complete_settlements)
    tampered[0] = tampered[0].model_copy(
        update={"cost_usd_micros": tampered[0].cost_usd_micros + 1}
    )
    with pytest.raises(ValueError):
        derive_model_evidence(policy, binding, authority, tampered)
    with pytest.raises(ValueError):
        provider_settlement_digest(
            complete_settlements[0].model_copy(update={"router_attempts": []}),
            policy,
        )

    selected_failure = _settlements(vector, "provider_failure", policy)[0]
    assert selected_failure.usage_available and selected_failure.cost_available
    for field in ("usage_available", "cost_available"):
        with pytest.raises(ValueError):
            provider_settlement_digest(
                selected_failure.model_copy(update={field: False}), policy
            )
    pre_provider_retry = _settlements(vector, "retry_complete", policy)[0]
    assert not pre_provider_retry.usage_available
    assert not pre_provider_retry.cost_available
    for field in ("usage_available", "cost_available"):
        with pytest.raises(ValueError):
            provider_settlement_digest(
                pre_provider_retry.model_copy(update={field: True}), policy
            )


def test_known_field_projection_is_forward_compatible() -> None:
    vector = _load(_POLICY_VECTOR)
    original = _parse(CodingInferencePolicy, vector["policy"])
    policy = original
    extended = copy.deepcopy(vector["policy"])
    extended["future_unsigned_diagnostic"] = {
        "nested": ["preserved only outside the known-field projection"]
    }
    parsed = _parse(CodingInferencePolicy, extended)
    assert parsed == original
    assert policy_digest(parsed) == policy_digest(original)

    tools = _load(_MINER_VECTOR)["tool_schema"]
    extended_tools = copy.deepcopy(tools)
    extended_tools["tools"][0]["function"]["parameters"]["futureKeyword"] = {
        "enabled": True
    }
    assert tool_schema_digest(_parse(CodingInferenceToolSchema, extended_tools)) != (
        tool_schema_digest(_parse(CodingInferenceToolSchema, tools))
    )

    receipt = vector["receipt_sets"]["complete"]
    extended_receipt = copy.deepcopy(receipt)
    extended_receipt["receipts"][0]["future_unsigned_diagnostic"] = True
    assert receipt_set_digest(
        _parse(CodingInferenceReceiptSet, extended_receipt), policy
    ) == (receipt_set_digest(_parse(CodingInferenceReceiptSet, receipt), policy))


def test_raw_parser_rejects_ambiguous_or_unsafe_json() -> None:
    policy = _load(_POLICY_VECTOR)["policy"]
    raw = _body(policy)
    duplicate = raw.replace(
        b'{"schema":',
        b'{"schema":"other","schema":',
        1,
    )
    missing = copy.deepcopy(policy)
    missing.pop("model")
    deep = copy.deepcopy(policy)
    value: object = "leaf"
    for _ in range(34):
        value = [value]
    deep["future"] = value

    invalid_documents = [
        duplicate,
        _body(missing),
        raw + b" {}",
        raw[:-1] + b"\xff}",
        _body(deep),
        b" " * (MAX_CANONICAL_INFERENCE_BYTES + 1),
    ]
    for document in invalid_documents:
        with pytest.raises(ValueError):
            parse_coding_inference_json(CodingInferencePolicy, document)

    for number in (("9" * 101).encode(), b"1e101"):
        oversized_number = raw.replace(
            b'{"schema":', b'{"future_number":' + number + b',"schema":', 1
        )
        with pytest.raises(ValueError):
            parse_coding_inference_json(CodingInferencePolicy, oversized_number)
    bounded_number = raw.replace(b'{"schema":', b'{"future_number":1e100,"schema":', 1)
    parse_coding_inference_json(CodingInferencePolicy, bounded_number)

    wrong_type = copy.deepcopy(policy)
    wrong_type["max_requests"] = True
    with pytest.raises(ValueError):
        _parse(CodingInferencePolicy, wrong_type)

    tools = _body(_load(_MINER_VECTOR)["tool_schema"])
    for replacement in (b'"maximum":-0', b'"maximum":8.0', b'"maximum":8e0'):
        changed = tools.replace(b'"maximum":8', replacement, 1)
        with pytest.raises(ValueError):
            parse_coding_inference_json(CodingInferenceToolSchema, changed)


def test_receipt_sets_reject_identity_order_and_accounting_drift() -> None:
    vector = _load(_POLICY_VECTOR)
    valid = vector["receipt_sets"]["retry_complete"]
    mutations = []

    drift = copy.deepcopy(valid)
    drift["receipts"][1]["request_id"] = "77777777-7777-4777-8777-777777777777"
    mutations.append(drift)
    drift = copy.deepcopy(valid)
    drift["receipts"][1]["attempt"] = 3
    mutations.append(drift)
    drift = copy.deepcopy(valid)
    drift["receipts"].pop()
    mutations.append(drift)
    drift = copy.deepcopy(valid)
    drift["receipts"][1]["total_tokens"] += 1
    mutations.append(drift)
    drift = copy.deepcopy(valid)
    drift["receipts"][0]["schema"] = "other"
    mutations.append(drift)
    drift = copy.deepcopy(vector["receipt_sets"]["complete"])
    drift["receipts"][0]["provider_generation_id"] = None
    mutations.append(drift)
    drift = copy.deepcopy(valid)
    drift["receipts"][1]["provider_settlement_sha256"] = drift["receipts"][0][
        "provider_settlement_sha256"
    ]
    mutations.append(drift)
    drift = copy.deepcopy(vector["receipt_sets"]["complete"])
    drift["receipts"][1]["provider_generation_id"] = drift["receipts"][0][
        "provider_generation_id"
    ]
    mutations.append(drift)
    drift = copy.deepcopy(valid)
    drift["grant_id"] = "{" + drift["grant_id"] + "}"
    mutations.append(drift)
    drift = copy.deepcopy(vector["receipt_sets"]["complete"])
    drift["receipts"][0]["fallback_used"] = True
    mutations.append(drift)
    drift = copy.deepcopy(vector["receipt_sets"]["provider_failure"])
    drift["receipts"][0]["receipt_provider"] = None
    mutations.append(drift)
    drift = copy.deepcopy(vector["receipt_sets"]["provider_failure"])
    later = copy.deepcopy(vector["receipt_sets"]["complete"]["receipts"][1])
    later["sequence"] = 2
    later["request_sequence"] = 2
    drift["receipts"].append(later)
    mutations.append(drift)

    for mutation in mutations:
        with pytest.raises(ValueError):
            _parse(CodingInferenceReceiptSet, mutation)


def test_locked_request_and_response_cannot_escape_policy() -> None:
    vector = _load(_POLICY_VECTOR)
    policy = _parse(CodingInferencePolicy, vector["policy"])
    request = _parse(CodingInferenceLockedRequest, vector["locked_requests"][0])
    response = _parse(
        CodingInferenceNormalizedResponse,
        vector["normalized_provider_responses"][0],
    )

    drifted_request = request.model_copy(
        update={"max_completion_tokens": policy.max_completion_tokens_per_request + 1}
    )
    with pytest.raises(ValueError):
        locked_request_digest(drifted_request, policy)

    unknown_request = copy.deepcopy(vector["locked_requests"][0])
    unknown_request["provider_override"] = "forbidden"
    with pytest.raises(ValueError):
        _parse(CodingInferenceLockedRequest, unknown_request)
    unknown_message = copy.deepcopy(vector["locked_requests"][0])
    unknown_message["messages"][0]["future_model_visible_field"] = True
    with pytest.raises(ValueError):
        _parse(CodingInferenceLockedRequest, unknown_message)

    drifted_response = response.model_copy(
        update={
            "usage": response.usage.model_copy(
                update={"cost_usd_micros": policy.max_cost_usd_micros + 1}
            )
        }
    )
    with pytest.raises(ValueError):
        normalized_response_digest(drifted_response, policy)

    unicode_prompt = CodingInferenceSystemPrompt(
        schema="dittobench-coding-system-prompt-v1",
        content="Preserve café <tag> & separators \u2028 and \u2029.",
    )
    canonical = coding_inference_canonical_json_bytes(unicode_prompt)
    assert b"caf\xc3\xa9" in canonical
    assert b"\\u2028" in canonical and b"\\u2029" in canonical


def test_provider_cost_uses_exact_half_even_micros() -> None:
    vector = _load(_POLICY_VECTOR)
    policy = _parse(CodingInferencePolicy, vector["policy"])
    base = vector["provider_responses"][0]
    for raw, expected in ((0.0000005, 0), (0.0000015, 2), (1e-6, 1)):
        changed = copy.deepcopy(base)
        changed["usage"]["cost"] = raw
        normalized = normalize_provider_response(
            _parse(CodingInferenceProviderResponse, changed), policy
        )
        assert normalized.usage.cost_usd_micros == expected
    for raw in (-0.1, 100.000001):
        changed = copy.deepcopy(base)
        changed["usage"]["cost"] = raw
        with pytest.raises(ValueError):
            _parse(CodingInferenceProviderResponse, changed)


def test_inference_counters_share_the_uint64_boundary() -> None:
    vector = _load(_POLICY_VECTOR)
    evidence = copy.deepcopy(vector["model_evidence"]["complete"])
    evidence["prompt_tokens"] = 1 << 64
    evidence["total_tokens"] = evidence["prompt_tokens"] + evidence["completion_tokens"]
    with pytest.raises(ValueError):
        _parse(CodingInferenceModelEvidence, evidence)

    response = copy.deepcopy(vector["normalized_provider_responses"][0])
    response["usage"]["prompt_tokens"] = 1 << 64
    response["usage"]["total_tokens"] = (
        response["usage"]["prompt_tokens"] + response["usage"]["completion_tokens"]
    )
    with pytest.raises(ValueError):
        _parse(CodingInferenceNormalizedResponse, response)
