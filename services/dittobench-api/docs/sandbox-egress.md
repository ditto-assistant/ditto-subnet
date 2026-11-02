# Sandbox egress proof

**Status: activation prerequisite for bench v13.** The block-bound
confirmation seeds shipped for v13 (`ditto/validator/crn.py`,
`apps/platform/ditto/api_server/confirmation_seed_anchor.py`) make a reign's
confirmation datasets unknowable *before* the reign's anchor block is finalized.
They do nothing about a harness that regenerates the dataset *during* the run:
the generator is public, the seed is served on the lease, and Platform publishes
`dataset_seed` / `seed_block` per score. The only thing standing between a
published seed and a live answer key is that the untrusted harness cannot reach
anything but the trusted broker. This document records that boundary, the tests
that pin each layer of it, and what an operator must verify on every scoring
validator before v13 sets weights.

If any layer below is found open on a scoring validator, the unregenerable
surface pass (issue *Unregenerable surface pass for v13.0*) escalates from
blocker-with-options to **blocker-with-salt**: only a validator-drawn
commit-reveal salt (option A) is unregenerable against a harness with egress,
because a Platform-side private pass still hands the harness a public seed.

## The claim

An untrusted harness container, while scored, can open connections to exactly
two destinations, both owned by the validator stack:

1. **the ticket-bound inference broker / relay** — platform inference, the
   locked embedding operation, and the observed-execution tool endpoint, on the
   source-bound broker port (`11436`) at `host.docker.internal` and the
   ticket-scoped `c-<id>.host.docker.internal` capability alias;
2. **the hardcoded `https://openrouter.ai` shim** — resolved inside the sandbox
   to the same host gateway and NAT-redirected into the same source-bound
   broker session (`scripts/sandbox-docker-entrypoint.sh`), so a kit that still
   dials the provider by name lands on the relay, not on the internet.

Everything else — cloud metadata, loopback services on the DinD host
(`dockerd :2375`, scorer `:8000`), sibling bridges, RFC1918, public internet,
and direct DNS — is refused. On the relay itself neither the model nor the
provider/route is miner-controllable, so *which upstream answered* cannot carry
information out either.

## The layers, and the test that pins each

| Layer | Where | Proof |
|---|---|---|
| Container run args: egress-restricted `--network`, `--cap-drop ALL`, loopback-only publish, exactly two `--add-host` names, `HTTPS_PROXY`/`HTTP_PROXY` forced with a closed `NO_PROXY` | `internal/sandbox/sandbox.go` `runArgsForNetwork` | `TestRunArgs_EgressProofRelayAndBrokerOnly`, `TestRunArgs_BrokerCapabilityHostUsesGatewayAndBypassesProxy`, `TestRunArgs_OpenRouterShimUsesHostGatewayAndPublicCABundle` (`internal/sandbox/sandbox_run_test.go`) |
| The switch: no `DITTOBENCH_SANDBOX_EGRESS_NETWORK` means the default full-egress bridge | same | `TestRunArgs_NoEgressNetworkIsTheUnrestrictedFallback` |
| Host firewall (managed compose stack): `DITTO-SANDBOX-EGRESS` on `DOCKER-USER` and `INPUT` for `ditto-sandbox0` and every per-run `dtj+` bridge; only the broker port and the shim port are accepted, everything else `REJECT`s | `scripts/sandbox-docker-entrypoint.sh` `ensure_sandbox_network` | `ditto/tests/test_validator_compose.py`, `ditto/tests/test_compose_stack.py` (stack shape); the entrypoint is idempotent and self-healing on every daemon start |
| Host firewall (bare-metal Ansible role): `DITTO-SANDBOX` chain drops every forward from the sandbox subnet except to the allowlisting proxy; `DITTO-SANDBOX-INPUT` admits only the broker (and optionally embeddings) | `infra/ansible/roles/dittobench/templates/ditto-sandbox-firewall.sh.j2` | operator converge; see *Operator verification* |
| Allowlisting CONNECT proxy: hostname allowlist at CONNECT time, non-CONNECT refused, IP literals / metadata / lookalike domains refused, **empty allowlist denies everything** | `cmd/egress-proxy/main.go` | `TestHostAllowed`, `TestEmptyAllowlistDeniesAll`, `TestServeHTTP_MetadataLoopbackAndLookalikesDenied`, `TestServeHTTP_EmptyAllowlistDeniesTheRelayUpstreamToo`, `TestTunnel_EndToEnd` |
| Relay model lock: from bench v7 the served model is the grant's locked model whatever the request names; below v7 only the fixed `DITTO_INFERENCE_ALLOWED_MODELS` set (1–4 entries) is accepted | `services/model-relay/internal/inference/chat.go` `lockedGrantModel` | `TestLockedGrantModelIgnoresTheRequestedModelFromV7`, `TestLegacyGrantModelMustBeOnTheFixedAllowlist` (`model_lock_egress_test.go`) |
| Relay routing lock: `provider`, `route`, `preset` are dropped before the upstream call; `models`, `transforms`, `plugins`, `web_search_options` are refused; nothing shaped like an endpoint override is forwardable | `services/model-relay/internal/inference/schema.go` | `TestLockedUpstreamPayloadDropsMinerRoutingControls`, `TestRoutingFieldsThatCouldSteerTheUpstreamAreRefusedOrDropped`, `TestLockedUpstreamPayload` |
| Relay upstream pin: `DITTO_INFERENCE_UPSTREAM_URL` / embedding URLs must be the exact OpenRouter (or Perplexity fallback) endpoints or the relay refuses to boot; Platform `check_config` pins the same hosts | `services/model-relay/internal/config/config.go` `validateInferenceProxy`; `apps/platform/ditto/api_server/config.py` `check_config` | `services/model-relay/internal/config/config_test.go`; `apps/platform/ditto/tests/api_server/test_config.py` |

## What is *not* proven here

* **The bare-metal Ansible role is opt-in.** `dittobench_sandbox_egress_enabled`
  defaults to `false` and `dittobench_egress_allow` still lists `openrouter.ai`
  unless `validator_platform_inference_enforced` is true. A host converged with
  defaults runs the full-egress bridge. The managed compose stack
  (`docker-compose.yml`) sets `DITTOBENCH_SANDBOX_EGRESS_NETWORK: ditto-sandbox`
  and `VALIDATOR_SANDBOX_EGRESS_RESTRICTED: "1"` unconditionally.
* **A compromised validator host.** Heartbeat protocol 18 reports
  `VALIDATOR_SANDBOX_EGRESS_RESTRICTED` as signed routing data, not remote
  attestation (`docs/UNTRUSTED-EXECUTION-RUNBOOK.md`). A host that lies about
  its egress posture is outside this proof; it is the reason the surface-pass
  decision is *which*, not *whether*.
* **The screener sandbox.** Screening runs the public rehearsal dataset, never a
  scored seed, inside per-execution `--internal` networks with a fake gateway
  (`workers/screener/ditto_screener/gate.py`, `fake_gateway.py`). It is out of
  scope for the answer-key channel because there is no scored answer key to
  fetch there.
* **Side channels through the relay's *content*.** The relay carries the
  harness's prompts to the locked model. The prompt is the benchmark's own
  transcript; nothing here stops a harness from encoding data *into* a prompt.
  What the proof rules out is a second party reading that data: the upstream
  is Platform-owned and fixed, and the provider receipt returns only the
  completion.

## Operator verification (before v13 activation)

On every validator that will score v13:

```bash
# managed compose stack: the switch is set and the chain exists
docker compose config | grep -E 'DITTOBENCH_SANDBOX_EGRESS_NETWORK|VALIDATOR_SANDBOX_EGRESS_RESTRICTED'
docker compose exec sandbox-docker iptables -S DITTO-SANDBOX-EGRESS
docker compose exec sandbox-docker iptables -S DOCKER-USER | grep DITTO-SANDBOX-EGRESS
```

```bash
# bare-metal Ansible role: both flags on, so the proxy allowlist is EMPTY
grep -E 'dittobench_sandbox_egress_enabled|validator_platform_inference_enforced' \
  infra/ansible/host_vars/<host>.yml
sudo iptables -S DITTO-SANDBOX && sudo systemctl is-active ditto-egress-proxy
docker logs ditto-egress-proxy 2>&1 | grep -E 'allow hosts|empty allowlist'
```

A live probe from inside a throwaway harness container on the sandbox network
must fail closed for each of: `https://169.254.169.254/`, `http://172.17.0.1:2375/`,
`https://example.com/`, and a raw `nc -z 8.8.8.8 53`. The only successes are the
broker port and the `openrouter.ai` shim, both of which land on the relay.

Record the result of that probe in the v13 activation PR. If anything besides
the broker answers, do **not** activate v13 on the block-bound seeds alone; the
surface-pass follow-up becomes blocker-with-salt.
