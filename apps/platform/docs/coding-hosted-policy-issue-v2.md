# Hosted-v2 inference policy and budget profile issuance

`python -I -m ditto.coding_hosted_policy_issue` issues the time-bound half of a
hosted-v2 canary bundle: the native inference policy and the budget profile it
commits through `runtime_profile_sha256`. It binds them to one launch-checked
execution profile (see `services/dittobench-api/docs/coding-hosted-profiles-v2.md`)
and to the locked v1 prompt/tool ABI.

Run it only on an owner-controlled machine. Its outputs are review inputs, not an
approval, assignment, credential, provider probe or activation.

```text
python -I -m ditto.coding_hosted_policy_issue \
  --request /ABS/PRIVATE/policy-request.json \
  --execution-profile /ABS/PRIVATE/execution-profile.json \
  --locked-v1-policy /ABS/CHECKOUT/packages/dittobench-coding-contract/testdata/coding_inference_policy_locked_v1.json \
  --provider-review /ABS/PRIVATE/provider-review \
  --token-accounting-review /ABS/PRIVATE/token-accounting-review \
  --pricing-review /ABS/PRIVATE/pricing-review \
  --valid-from-unix 2000000000 --valid-seconds 86400 \
  --output /ABS/PRIVATE/NEW-DIRECTORY
```

## Inputs

The closed `dittobench-coding-hosted-policy-request-v1` request carries only
reviewed integers:
- policy caps: `max_requests`, `max_prompt_tokens`, `max_completion_tokens`,
  `max_completion_tokens_per_request`, `max_cost_usd_micros`,
  `request_timeout_milliseconds`;
- budget bounds: `max_billed_prompt_tokens_per_request`, the two unit prices in
  **USD nanodollars per token**, and `fixed_charge_usd_micros_per_request`;
- explicit `shadow_only: true` and `weight_eligible: false`.

The per-request completion cap is shared by the policy and the budget profile.

Everything else is fixed or derived:
- **Fixed by the models:** model, route, route profile, receipt provider,
  reasoning effort, flags, guardrails, retry policy and algorithm.
- **Prompt and tool-schema digests:** copied from the locked v1 policy file,
  which must hash to Platform's pinned locked policy digest. A request cannot
  choose a different ABI.
- **Review digests:** the SHA-256 of three distinct, non-empty evidence files.
  The digests identify the evidence and do not prove review. The approver must
  read the evidence itself.
- **Validity window:** `valid-from-unix` plus at most 86400 seconds. The runtime
  refuses the profile outside that window, so a live assignment needs a policy
  issued for the current day.

## Checks

1. The execution profile must be exact canonical JSON with positive budgets,
   wall time of at most one hour, and a tool-call budget equal to
   `CandidateLimits.MaxToolCalls`.
2. Both documents must validate against `HostedBudgetProfile` and
   `HostedInferencePolicy`.
3. `ProfiledBudgetEstimator`, the exact estimator the runtime constructs, must
   accept the pair at the window start. That enforces the digest binding, the
   billed prompt cap within the policy prompt cap, the per-request completion
   caps, and a full-request cost ceiling within `max_cost_usd_micros`.
4. The issuer computes the grant limits the Platform ledger will derive from
   the policy and execution profile (requests, prompt tokens, completion tokens,
   cost). At least one full-size request must be admissible under all of them,
   and the request timeout must fit within the wall time.

A rejection prints a fixed message, exits 70 and writes nothing. On success it
creates the new mode-`0700` directory and exclusively writes three mode-`0600`
files: `inference-policy.json`, `budget-profile.json` and `receipt.json`. It
prints only the receipt.

The receipt records:
- the policy, budget, execution-profile, locked-policy, ABI and review digests;
- the validity window;
- the derived grant limits, the full-request cost ceiling and the number of
  full-size requests admissible;
- `approved=false`.

## Not established here

Prices, billed-token caps and account posture come from the reviewed evidence,
not from this tool. As `coding-hosted-budget-v2.md` explains, the local
reservation is conditional on those bounds, and catalogue listings alone are not
proof. Assignment creation, credentials, provider access and every activation
gate remain separate.
