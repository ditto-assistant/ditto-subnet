"""Operator issuer for a hosted-v2 inference policy and its bound budget profile.

Run only on an owner-controlled machine. It binds reviewed caps, prices and
opaque review evidence to one launch-checked execution profile and the locked v1
prompt/tool ABI. Outputs are review inputs, never an approval or activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

REQUEST_SCHEMA = "dittobench-coding-hosted-policy-request-v1"
RECEIPT_SCHEMA = "dittobench-coding-hosted-policy-receipt-v1"
REQUEST_FIELDS = {
    "schema",
    "max_requests",
    "max_prompt_tokens",
    "max_completion_tokens",
    "max_completion_tokens_per_request",
    "max_cost_usd_micros",
    "request_timeout_milliseconds",
    "max_billed_prompt_tokens_per_request",
    "prompt_price_usd_nanos_per_token",
    "completion_price_usd_nanos_per_token",
    "fixed_charge_usd_micros_per_request",
    "shadow_only",
    "weight_eligible",
}
REVIEWS = ("provider_review", "token_accounting_review", "pricing_review")
MAX_REVIEW_BYTES = 4 << 20


class PolicyIssueError(ValueError):
    """Safe rejection without request, evidence or profile content."""


@dataclass(frozen=True)
class IssuedPolicy:
    policy: bytes
    budget_profile: bytes
    receipt: bytes


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _execution_budgets(execution_profile: bytes) -> tuple[dict[str, int], int]:
    from ditto.api_models.coding_canonical import coding_canonical_json_bytes
    from ditto.api_models.coding_inference import _decode_json_document

    profile = _decode_json_document(execution_profile, maximum_bytes=16384)
    if (
        not isinstance(profile, dict)
        or profile.get("schema") != "dittobench-coding-hosted-authoring-profile-v2"
        or coding_canonical_json_bytes(
            profile, maximum_bytes=16384, label="execution profile"
        )
        != execution_profile
    ):
        raise PolicyIssueError("execution profile is invalid")
    budgets = profile.get("budgets")
    names = (
        "model_input_tokens",
        "model_output_tokens",
        "workspace_tool_calls",
        "wall_time_seconds",
    )
    if (
        not isinstance(budgets, dict)
        or any(
            type(budgets.get(name)) is not int or budgets[name] <= 0 for name in names
        )
        or budgets["wall_time_seconds"] > 3600
    ):
        raise PolicyIssueError("execution profile budgets are invalid")
    policy = profile.get("resource_policy")
    limits = policy.get("CandidateLimits") if isinstance(policy, dict) else None
    if (
        not isinstance(limits, dict)
        or limits.get("MaxToolCalls") != budgets["workspace_tool_calls"]
    ):
        raise PolicyIssueError("execution profile tool budget disagrees")
    return {name: budgets[name] for name in names}, budgets["workspace_tool_calls"]


def _locked_abi(locked_v1_policy: bytes) -> tuple[str, str]:
    from ditto.api_models.coding_inference import (
        CodingInferencePolicy,
        parse_coding_inference_json,
        policy_digest,
    )
    from ditto.api_server.endpoints.validator_coding_inference import (
        _LOCKED_POLICY_SHA256,
    )

    policy = parse_coding_inference_json(CodingInferencePolicy, locked_v1_policy)
    if policy_digest(policy) != _LOCKED_POLICY_SHA256:
        raise PolicyIssueError("locked v1 policy digest differs")
    return policy.prompt_sha256, policy.tool_schema_sha256


def issue(
    *,
    request: dict[str, Any],
    execution_profile: bytes,
    locked_v1_policy: bytes,
    reviews: dict[str, bytes],
    valid_from_unix: int,
    valid_seconds: int,
) -> IssuedPolicy:
    from ditto.api_models.coding_canonical import coding_canonical_json_bytes
    from ditto.api_models.coding_hosted_budget import HostedBudgetProfile
    from ditto.api_models.coding_hosted_inference import HostedInferencePolicy
    from ditto.api_models.coding_inference import effective_inference_request_budget
    from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator

    if (
        not isinstance(request, dict)
        or set(request) != REQUEST_FIELDS
        or request["schema"] != REQUEST_SCHEMA
        or request["shadow_only"] is not True
        or request["weight_eligible"] is not False
        or any(
            type(request[name]) is not int
            for name in REQUEST_FIELDS - {"schema", "shadow_only", "weight_eligible"}
        )
    ):
        raise PolicyIssueError("policy request is invalid")
    if (
        set(reviews) != set(REVIEWS)
        or any(not 0 < len(body) <= MAX_REVIEW_BYTES for body in reviews.values())
        or len({_sha(body) for body in reviews.values()}) != len(REVIEWS)
    ):
        raise PolicyIssueError("review evidence is invalid")
    if (
        type(valid_from_unix) is not int
        or type(valid_seconds) is not int
        or valid_from_unix <= 0
        or not 0 < valid_seconds <= 86400
    ):
        raise PolicyIssueError("validity window is invalid")
    try:
        budgets, tool_calls = _execution_budgets(execution_profile)
        prompt_sha256, tool_schema_sha256 = _locked_abi(locked_v1_policy)
    except ValueError:
        raise PolicyIssueError(
            "execution profile or locked policy is invalid"
        ) from None
    try:
        budget = HostedBudgetProfile.model_validate(
            {
                "schema": "dittobench-coding-hosted-budget-profile-v2",
                "algorithm": "provider-billed-input-cap-v1",
                "model": "openai/gpt-5.6-luna",
                "provider_api": "openrouter",
                "provider_route": "azure/eu",
                "provider_route_profile": "luna-azure-eu-zdr-v1",
                "provider_review_sha256": _sha(reviews["provider_review"]),
                "token_accounting_review_sha256": _sha(
                    reviews["token_accounting_review"]
                ),
                "pricing_review_sha256": _sha(reviews["pricing_review"]),
                "valid_from_unix": valid_from_unix,
                "valid_until_unix": valid_from_unix + valid_seconds,
                "max_billed_prompt_tokens_per_request": request[
                    "max_billed_prompt_tokens_per_request"
                ],
                "max_completion_tokens_per_request": request[
                    "max_completion_tokens_per_request"
                ],
                "prompt_price_usd_nanos_per_token": request[
                    "prompt_price_usd_nanos_per_token"
                ],
                "completion_price_usd_nanos_per_token": request[
                    "completion_price_usd_nanos_per_token"
                ],
                "fixed_charge_usd_micros_per_request": request[
                    "fixed_charge_usd_micros_per_request"
                ],
            }
        )
        policy = HostedInferencePolicy.model_validate(
            {
                "schema": "dittobench-coding-hosted-inference-policy-v2",
                "model": "openai/gpt-5.6-luna",
                "provider_api": "openrouter",
                "provider_route": "azure/eu",
                "receipt_provider": "Azure",
                "provider_route_profile": "luna-azure-eu-zdr-v1",
                "reasoning_effort": "medium",
                "prompt_sha256": prompt_sha256,
                "tool_schema_sha256": tool_schema_sha256,
                "allow_fallbacks": False,
                "stream": False,
                "store": False,
                "zdr": True,
                "data_collection": "deny",
                "require_parameters": True,
                "provider_account_guardrail": "openrouter_private_account_v1",
                "provider_pipeline_policy": "no_plugins_no_transforms_v1",
                "provider_cache_policy": "disabled_v1",
                "router_metadata_required": True,
                "retry_policy": "no_retries_v2",
                "runtime_profile_sha256": budget.digest(),
                "max_requests": request["max_requests"],
                "max_prompt_tokens": request["max_prompt_tokens"],
                "max_completion_tokens": request["max_completion_tokens"],
                "max_completion_tokens_per_request": request[
                    "max_completion_tokens_per_request"
                ],
                "max_cost_usd_micros": request["max_cost_usd_micros"],
                "request_timeout_milliseconds": request["request_timeout_milliseconds"],
            }
        )
        budget_bytes = budget.canonical_bytes()
        # The exact estimator the runtime constructs, evaluated at window start.
        ProfiledBudgetEstimator(
            profile_bytes=budget_bytes, policy=policy, now=lambda: valid_from_unix
        )
    except ValueError:
        raise PolicyIssueError("policy or budget profile is invalid") from None
    # The Platform ledger grants these limits for the bound execution profile.
    request_limit = min(
        policy.max_requests, effective_inference_request_budget(tool_calls)
    )
    prompt_limit = min(policy.max_prompt_tokens, budgets["model_input_tokens"])
    completion_limit = min(policy.max_completion_tokens, budgets["model_output_tokens"])
    request_ceiling = budget.cost_ceiling(policy.max_completion_tokens_per_request)
    full_requests = min(
        request_limit,
        prompt_limit // budget.max_billed_prompt_tokens_per_request,
        completion_limit // policy.max_completion_tokens_per_request,
        policy.max_cost_usd_micros // request_ceiling,
    )
    if (
        full_requests < 1
        or policy.request_timeout_milliseconds > budgets["wall_time_seconds"] * 1000
    ):
        raise PolicyIssueError("no full request is admissible within the grant")
    policy_bytes = coding_canonical_json_bytes(
        policy.model_dump(mode="json", by_alias=True),
        maximum_bytes=16384,
        label="hosted inference policy",
    )
    receipt = coding_canonical_json_bytes(
        {
            "schema": RECEIPT_SCHEMA,
            "policy_sha256": policy.digest(),
            "runtime_profile_sha256": budget.digest(),
            "execution_profile_sha256": _sha(execution_profile),
            "locked_v1_policy_sha256": _sha(locked_v1_policy),
            "prompt_sha256": prompt_sha256,
            "tool_schema_sha256": tool_schema_sha256,
            "provider_review_sha256": budget.provider_review_sha256,
            "token_accounting_review_sha256": budget.token_accounting_review_sha256,
            "pricing_review_sha256": budget.pricing_review_sha256,
            "valid_from_unix": budget.valid_from_unix,
            "valid_until_unix": budget.valid_until_unix,
            "grant_request_limit": request_limit,
            "grant_prompt_token_limit": prompt_limit,
            "grant_completion_token_limit": completion_limit,
            "grant_cost_limit_usd_micros": policy.max_cost_usd_micros,
            "full_request_cost_ceiling_usd_micros": request_ceiling,
            "full_requests_admissible": full_requests,
            "approved": False,
            "shadow_only": True,
            "weight_eligible": False,
        },
        maximum_bytes=16384,
        label="hosted policy receipt",
    )
    return IssuedPolicy(
        policy=policy_bytes, budget_profile=budget_bytes, receipt=receipt
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise PolicyIssueError("policy issuer arguments invalid")


def _read(path: Path, maximum: int) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PolicyIssueError("input path is invalid")
    body = path.read_bytes()
    if len(body) > maximum:
        raise PolicyIssueError("input is too large")
    return body


def _write_outputs(output: Path, issued: IssuedPolicy) -> None:
    if not output.is_absolute() or os.path.lexists(output):
        raise PolicyIssueError("output must be a new absolute directory")
    output.mkdir(mode=0o700)
    for name, body in (
        ("inference-policy.json", issued.policy),
        ("budget-profile.json", issued.budget_profile),
        ("receipt.json", issued.receipt),
    ):
        descriptor = os.open(
            output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(descriptor, body)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _Parser(add_help=False)
        parser.add_argument("--request", type=Path, required=True)
        parser.add_argument("--execution-profile", type=Path, required=True)
        parser.add_argument("--locked-v1-policy", type=Path, required=True)
        for review in REVIEWS:
            parser.add_argument(
                f"--{review.replace('_', '-')}", type=Path, required=True
            )
        parser.add_argument("--valid-from-unix", type=int, required=True)
        parser.add_argument("--valid-seconds", type=int, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv)
        try:
            request = json.loads(_read(args.request, 1 << 20))
        except json.JSONDecodeError:
            raise PolicyIssueError("policy request is not JSON") from None
        issued = issue(
            request=request,
            execution_profile=_read(args.execution_profile, 16384),
            locked_v1_policy=_read(args.locked_v1_policy, 1 << 20),
            reviews={
                review: _read(getattr(args, review), MAX_REVIEW_BYTES)
                for review in REVIEWS
            },
            valid_from_unix=args.valid_from_unix,
            valid_seconds=args.valid_seconds,
        )
        _write_outputs(args.output, issued)
    except (ValueError, OSError):
        print("hosted policy request rejected", file=sys.stderr)
        return 70
    sys.stdout.buffer.write(issued.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
