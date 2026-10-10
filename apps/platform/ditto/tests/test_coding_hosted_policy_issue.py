"""Synthetic issuer tests; no provider, price source, credential or database."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_budget import HostedBudgetProfile
from ditto.api_models.coding_hosted_inference import HostedInferencePolicy
from ditto.api_models.coding_inference import parse_coding_inference_json
from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator
from ditto.coding_hosted_policy_issue import (
    PolicyIssueError,
    issue,
    main,
)

REPO = Path(__file__).resolve().parents[4]
LOCKED = (
    REPO / "packages/dittobench-coding-contract/testdata"
    "/coding_inference_policy_locked_v1.json"
)
START = 2_000_000_000


def execution_profile(**budgets) -> bytes:
    value = {
        "model_input_tokens": 200000,
        "model_output_tokens": 32000,
        "workspace_tool_calls": 128,
        "wall_time_seconds": 600,
    }
    value.update(budgets)
    return coding_canonical_json_bytes(
        {
            "schema": "dittobench-coding-hosted-authoring-profile-v2",
            "image_digest": "sha256:" + "9" * 64,
            "budgets": value,
            "resource_policy": {"CandidateLimits": {"MaxToolCalls": 128}},
        },
        maximum_bytes=16384,
        label="execution profile",
    )


def request(**changes) -> dict:
    value = {
        "schema": "dittobench-coding-hosted-policy-request-v1",
        "max_requests": 64,
        "max_prompt_tokens": 200000,
        "max_completion_tokens": 32000,
        "max_completion_tokens_per_request": 4096,
        "max_cost_usd_micros": 2_000_000,
        "request_timeout_milliseconds": 120000,
        "max_billed_prompt_tokens_per_request": 20000,
        "prompt_price_usd_nanos_per_token": 201,
        "completion_price_usd_nanos_per_token": 1201,
        "fixed_charge_usd_micros_per_request": 7,
        "shadow_only": True,
        "weight_eligible": False,
    }
    value.update(changes)
    return value


def reviews() -> dict[str, bytes]:
    return {
        "provider_review": b"synthetic provider review\n",
        "token_accounting_review": b"synthetic token accounting review\n",
        "pricing_review": b"synthetic pricing review\n",
    }


def issued(**overrides):
    arguments = {
        "request": request(),
        "execution_profile": execution_profile(),
        "locked_v1_policy": LOCKED.read_bytes(),
        "reviews": reviews(),
        "valid_from_unix": START,
        "valid_seconds": 86400,
    }
    arguments.update(overrides)
    return issue(**arguments)


def test_issued_policy_binds_budget_profile_abi_and_execution_profile() -> None:
    result = issued()
    budget = parse_coding_inference_json(
        HostedBudgetProfile, result.budget_profile, maximum_bytes=16384
    )
    policy = HostedInferencePolicy.model_validate_json(result.policy)
    receipt = json.loads(result.receipt)
    locked = json.loads(LOCKED.read_bytes())
    assert budget.canonical_bytes() == result.budget_profile
    assert policy.runtime_profile_sha256 == budget.digest()
    assert (policy.prompt_sha256, policy.tool_schema_sha256) == (
        locked["prompt_sha256"],
        locked["tool_schema_sha256"],
    )
    assert budget.valid_until_unix - budget.valid_from_unix == 86400
    for name, body in reviews().items():
        assert getattr(budget, f"{name}_sha256") == hashlib.sha256(body).hexdigest()
    # The runtime loader constructs this exact estimator inside the window.
    ProfiledBudgetEstimator(
        profile_bytes=result.budget_profile, policy=policy, now=lambda: START + 1
    )
    assert receipt["policy_sha256"] == policy.digest()
    assert receipt["runtime_profile_sha256"] == budget.digest()
    assert (
        receipt["execution_profile_sha256"]
        == hashlib.sha256(execution_profile()).hexdigest()
    )
    ceiling = (20000 * 201 + 999) // 1000 + (4096 * 1201 + 999) // 1000 + 7
    assert receipt["full_request_cost_ceiling_usd_micros"] == ceiling
    assert receipt["grant_request_limit"] == 64
    assert receipt["full_requests_admissible"] == min(
        64, 200000 // 20000, 32000 // 4096, 2_000_000 // ceiling
    )
    assert receipt["approved"] is False
    assert receipt["shadow_only"] is True and receipt["weight_eligible"] is False
    assert result == issued()


@pytest.mark.parametrize(
    ("changes", "profile"),
    [
        pytest.param({"extra": 1}, None, id="unknown-field"),
        pytest.param({"weight_eligible": True}, None, id="weight-eligible"),
        pytest.param({"max_requests": True}, None, id="boolean-integer"),
        pytest.param({"max_requests": 257}, None, id="request-bound"),
        pytest.param(
            {"max_completion_tokens_per_request": 32001}, None, id="per-request-total"
        ),
        pytest.param(
            {"request_timeout_milliseconds": 1500}, None, id="fractional-timeout"
        ),
        pytest.param(
            {"max_billed_prompt_tokens_per_request": 200001}, None, id="billed-cap"
        ),
        pytest.param({"max_cost_usd_micros": 1000}, None, id="cost-below-request"),
        pytest.param({"prompt_price_usd_nanos_per_token": 0}, None, id="zero-price"),
        pytest.param(
            {},
            execution_profile(model_input_tokens=10000),
            id="profile-input-too-small",
        ),
        pytest.param(
            {}, execution_profile(model_output_tokens=1000), id="profile-output-small"
        ),
        pytest.param(
            {"request_timeout_milliseconds": 300000},
            execution_profile(wall_time_seconds=120),
            id="timeout-over-wall",
        ),
        pytest.param({}, execution_profile(workspace_tool_calls=64), id="tool-drift"),
        pytest.param({}, execution_profile()[:-1], id="noncanonical-profile"),
    ],
)
def test_invalid_requests_and_unusable_grants_are_rejected(changes, profile) -> None:
    body = request()
    body.update(changes)
    overrides = {"request": body}
    if profile is not None:
        overrides["execution_profile"] = profile
    with pytest.raises(PolicyIssueError):
        issued(**overrides)


def test_locked_abi_evidence_and_window_are_enforced() -> None:
    locked = json.loads(LOCKED.read_bytes())
    locked["prompt_sha256"] = "0" * 64
    with pytest.raises(PolicyIssueError):
        issued(locked_v1_policy=json.dumps(locked).encode())
    duplicate = reviews()
    duplicate["pricing_review"] = duplicate["provider_review"]
    empty = reviews()
    empty["pricing_review"] = b""
    missing = reviews()
    missing.pop("token_accounting_review")
    for evidence in (duplicate, empty, missing):
        with pytest.raises(PolicyIssueError):
            issued(reviews=evidence)
    for window in (
        {"valid_seconds": 86401},
        {"valid_seconds": 0},
        {"valid_from_unix": 0},
    ):
        with pytest.raises(PolicyIssueError):
            issued(**window)


def test_cli_writes_owner_only_outputs_once_and_rejects_quietly(
    tmp_path, capsysbinary
) -> None:
    tmp_path.chmod(0o700)
    paths = {
        "request": tmp_path / "request.json",
        "execution-profile": tmp_path / "execution-profile.json",
        "provider-review": tmp_path / "provider.txt",
        "token-accounting-review": tmp_path / "tokens.txt",
        "pricing-review": tmp_path / "pricing.txt",
    }
    paths["request"].write_text(json.dumps(request()))
    paths["execution-profile"].write_bytes(execution_profile())
    for flag, name in (
        ("provider-review", "provider_review"),
        ("token-accounting-review", "token_accounting_review"),
        ("pricing-review", "pricing_review"),
    ):
        paths[flag].write_bytes(reviews()[name])
    output = tmp_path / "issued"
    argv = [
        *(item for flag, path in paths.items() for item in (f"--{flag}", str(path))),
        "--locked-v1-policy",
        str(LOCKED),
        "--valid-from-unix",
        str(START),
        "--valid-seconds",
        "3600",
        "--output",
        str(output),
    ]
    assert main(argv) == 0
    receipt = capsysbinary.readouterr().out
    assert receipt == (output / "receipt.json").read_bytes()
    assert sorted(p.name for p in output.iterdir()) == [
        "budget-profile.json",
        "inference-policy.json",
        "receipt.json",
    ]
    assert all(p.stat().st_mode & 0o077 == 0 for p in (output, *output.iterdir()))
    policy = HostedInferencePolicy.model_validate_json(
        (output / "inference-policy.json").read_bytes()
    )
    assert policy.digest() == json.loads(receipt)["policy_sha256"]
    assert main(argv) == 70
    assert capsysbinary.readouterr() == (b"", b"hosted policy request rejected\n")
    bad = copy.deepcopy(request())
    bad["max_cost_usd_micros"] = 1
    paths["request"].write_text(json.dumps(bad))
    argv[argv.index(str(output))] = str(tmp_path / "rejected")
    assert main(argv) == 70
    assert not (tmp_path / "rejected").exists()
    assert capsysbinary.readouterr() == (b"", b"hosted policy request rejected\n")
    assert main(["--request", "relative.json"]) == 70
