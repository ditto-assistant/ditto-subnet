# Locked Luna relay contract

DittoBench Coding v1 now has a canonical preimage for
`inference_grant_sha256`. The contract is public and synthetic; it is not a
ticket capability, grant exchange, provider endpoint, or credential.

## Split vector boundary

`coding_inference_miner_v1.json` contains only miner-visible material: the
fixed public system prompt, ordered coding-tool schemas, two non-streaming Chat
Completions turns, and miner-facing responses. The Rust reference harness may
consume this vector.

`coding_inference_policy_v1.json` contains validator/curator authority: the
locked provider policy, normalized provider responses, ordered settlement
receipts, evidence projections, and expected digests. Rust and miner-facing
code must not consume it. Neither vector contains a URL, bearer, key, broker
public key, live grant capability, or usable ticket.

## Policy digest

The policy fixes:

- `openai/gpt-5.6-luna` over OpenAI-compatible Chat Completions;
- the reviewed `azure/eu` route and `luna-azure-eu-zdr-v1` profile;
- medium reasoning, one non-streaming choice, exactly one tool decision per
  response, serial tool execution, and included usage; requests disable
  parallel calls and multi-call responses fail closed;
- provider fallback disabled, supported-parameter enforcement, denied data
  collection, and required ZDR;
- request, response, per-call output, aggregate token, cost, timeout, request,
  attempt, and retry ceilings;
- the public system-prompt and ordered tool-schema digests;
- provider-receipt cost in integer micro-USD.

Provider names are intentionally not overloaded:

| Field | Meaning |
|---|---|
| `provider_api` | OpenRouter, the API/accounting authority |
| `provider_route` | `azure/eu`, the exact route selector and `ModelEvidence.provider` value |
| `receipt_provider` | `Azure`, the case-sensitive selected-provider settlement identity |
| `provider_route_profile` | versioned calibration plus privacy/routing contract |

The canonical known-field policy projection is sorted-key compact UTF-8 JSON
with one trailing newline. Unknown additive fields are excluded; missing or
duplicate known fields, invalid Unicode/nesting, trailing data, and invalid
bounds fail. Its SHA-256 is `inference_grant_sha256`.

`openrouter_private_account_v1` is a requirement, not a self-attestation. A
future Platform integration must prove prompt/content logging is disabled,
response caching is disabled at account and request level, default plugins are
disabled, router metadata is enabled, and no unexpected pipeline or fallback
stage ran. `no_plugins_no_transforms_v1`, `disabled_v1`, and
`platform_settlement_v1` name those exact checks. Until a dedicated integration
returns a content-addressed settlement satisfying them, no live response may
claim this policy.

The prompt digest covers the exact fixed system-prompt projection. The tool
digest covers every ordered model-visible function name, description, and
nested JSON Schema field. Dynamic task, memory, assistant, and tool-result
messages are committed by each receipt's full locked-request digest instead of
being confused with the fixed prompt contract.

## Receipt and evidence rules

One receipt represents one trusted upstream attempt. Global sequence,
logical-request sequence, and per-request attempt are contiguous. Receipts stay
in dispatch order, never provider completion order. A receipt binds the exact
locked request, fixed prompt/tool contracts, model, route profile, response
digest when present, terminal status, provider selection, usage, cost, and
timeout state. `locked_request_sha256` always names the provider-facing locked
request; the miner-safe vector's separate `request_sha256` names the unprivileged
request before route controls are inserted.

Every receipt also references a canonical
`dittobench-coding-provider-settlement-v1` projection. That projection binds the
ticket/case/profile, policy and live grant generation, request/attempt identity,
actual selected provider, router attempts, empty pipeline, cache/privacy
guardrail revisions, response digest kind, usage, cost, and terminal result.
Evidence derivation requires one ordered settlement per receipt and re-hashes
each settlement; arbitrary digest labels, missing rows, reordering, and reuse
fail closed.

Only a proved pre-provider attempt with no selected provider, response, usage,
or cost may be `receipt_free_retry`. It must be followed by another attempt for
the same request identity and bytes. Completed and provider-failure receipts
are terminal. Any fallback, provider/model/profile drift, missing settlement,
ambiguous billing, duplicate identity, sequence gap, or budget overflow makes
evidence unavailable rather than manufacturing a normal failure. Usage and
cost availability are asserted independently; any selected-provider terminal
receipt requires both to be settled, even when the authoritative value is zero.

The provider-receipt-set digest is the canonical receipt-set projection for one
synthetic ticket/case/profile, policy digest, synthetic grant ID, and grant
generation with receipts in authoritative order. `not_invoked` has no receipt
root and zero accounting. The receipt set also binds the lower task-specific
request, prompt-token, and completion-token budgets; request count is
`min(workspace_tool_calls + 16, 256)`. `complete` needs every logical request to
complete. Any trusted terminal provider failure makes
the aggregate `provider_failure`; successful earlier calls remain in its
usage. Retry count is receipt count minus logical request count.

## Activation boundary

This PR defines replayable bytes only. It adds no listener, OpenRouter client,
API key, Platform grant row, exchange endpoint, source-bound capability,
harness lifecycle, worker, score, or weight path. The next layer may implement
an unwired `codingrelay` core against this contract. Dedicated Platform/model-
relay persistence and ticket exchange remain separate reviews.

The existing ordinary `model-relay` is only a transport reference. Its current
grant table is tied to core validator tickets, its reasoning lock is not Luna's,
and its aggregate route can use different fallback/recovery semantics. It must
not issue coding ModelEvidence or claim conformance to this policy. Activation
also requires an authenticated route preflight proving every locked parameter,
including serial tool-call control, is supported by the exact endpoint.

## Regeneration and validation

```bash
python3 packages/dittobench-coding-contract/generate_inference_vectors.py --check
cd services/dittobench-api && go test -race ./internal/codingcontract
cd miners/dittobench-coding-starter-kit && \
  cargo fmt --check && \
  cargo clippy --locked --all-targets --all-features -- -D warnings && \
  cargo test --locked --all-targets --all-features
```
