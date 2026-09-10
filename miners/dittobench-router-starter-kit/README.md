# DittoBench router starter kit

This is the reference **router** miner, published as the router competition's
default starter configuration. Out of the box it is a **faithful
pass-through**: it forwards every provider request to the validator relay
byte-for-byte and streams the response back unchanged. It also ships one
small, clearly-commented, deterministic example lever — archetype rerouting,
**off by default** — so you can see where a router earns its savings and how
to extend it.

Router contract v1 is a **shadow-only** competition dimension. It is
permanently `weight_eligible=false`: it does not add a project to the active
upload protocol, run a scored arm on its own, change the Tool + Memory
composite, or move subnet weights. See the authoritative interface spec in
[`docs/router-compression-competition-v1.md`](../../docs/router-compression-competition-v1.md)
(epic: <https://github.com/ditto-assistant/ditto-subnet/issues/1664>).

## Trust boundary

The scored router runs in a sandbox with a read-only rootfs, no writable mount
except a tmpfs, and **exactly one upstream: the validator relay**. The relay
holds the real provider credentials, resolves catalog route ids to providers,
enforces the frozen catalog and the arm's budget, and logs every upstream
body. This image contains **no provider credential** and reads none; its only
secret is the single-use relay ticket, which is injected at run time and never
logged.

```text
harness container ──► ingress tap ──► miner router container ──► relay ──► provider
   (Claude Code /        (validator,       (this kit, no other       (validator,
    Codex, pinned)        records every     egress)                    logs every
                          harness request,                             upstream body)
                          stamps X-Dittobench-Step)
```

The harness's `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` point at the validator's
**ingress tap**, which records each request byte-for-byte, stamps an
`X-Dittobench-Step` header (session, turn, request index), and forwards to this
router unchanged. The router echoes that step header onto its upstream relay
call so the validator can line each upstream body up with its tap counterpart.
With the tap and relay logs the validator computes what the router did to every
request without any cooperation from the miner — there is no self-declared
transform array to trust.

## Endpoints

```text
GET  /router/health              advertisement (contract versions, wires served)
POST /v1/messages                Anthropic Messages (streaming and non-streaming)
POST /v1/messages/count_tokens   Anthropic token counting
POST /v1/chat/completions        OpenAI Chat Completions (streaming, tool calls)
POST /v1/responses               OpenAI Responses
POST /router/seed                validator-supplied task memory records (may be empty)
```

`GET /router/health` returns exactly:

```json
{
  "status": "ok",
  "supported_router_contract_versions": [1],
  "wires": ["anthropic_messages", "openai_chat", "openai_responses"],
  "count_tokens": true
}
```

A `404` at `/router/health` means "no router project": no penalty, no router
score. The four provider routes are faithful pass-throughs — streaming SSE is
relayed event-for-event, and the provider `usage` (including the final
streamed chunk) is passed back untouched. Without a configured relay the
provider routes return `503`; `/router/health` and `/router/seed` still work,
which is what `--local-practice` is for.

## Environment

| Variable | Purpose |
|---|---|
| `DITTOBENCH_RELAY_BASE_URL` | The ticket-scoped relay origin (validator-injected). |
| `DITTOBENCH_RELAY_TICKET` | Single-use relay bearer (validator-injected; never logged). |
| `DITTOBENCH_ROUTER_REROUTE_ASIDES` | `1`/`true` to enable the example lever (default off). |
| `DITTOBENCH_ROUTER_ASIDE_MODEL` | Catalog route id to reroute cheap asides to. |

See [`.env.example`](.env.example). Scored runs receive the relay values from
the validator; you never set them by hand.

## Run locally

```bash
# Faithful pass-through against a relay (record/practice mode).
DITTOBENCH_RELAY_BASE_URL=http://127.0.0.1:9000 \
DITTOBENCH_RELAY_TICKET=practice-ticket \
cargo run --bin dittobench-router-miner -- --port 8080

# Health-only practice, no relay, loopback bind:
cargo run --bin dittobench-router-miner -- --local-practice --port 8080
curl -s localhost:8080/router/health
```

Scored runs bind `0.0.0.0` so the tap can reach the router; `--local-practice`
binds loopback only and tolerates a missing relay.

## The example lever: archetype rerouting (off by default)

The default router changes nothing. When you enable the lever
(`--reroute-asides` with `--aside-model <catalog route id>`, or the matching
env vars), it demonstrates the cheapest, safest savings a router can make:

- It detects a **cheap-shaped** Anthropic Messages request — the small,
  tool-free, short-`max_tokens`, single-user-turn "aside" the harness sends
  for title/probe generation — using request **shape only**, never harness
  wording (see [`src/reroute.rs`](src/reroute.rs)).
- It rewrites **only** the top-level `model` field to your cheaper catalog
  route id, leaving every other byte of the request identical (a surgical
  value splice, not a JSON re-serialization). Byte-stability keeps the
  provider prefix cache hitting and satisfies the determinism and
  prefix-stability gates.

This is the design doc's "shape-based request archetypes + per-archetype
routes (asides/probes → GLM 5.3 Flash)" lever, where a title prompt is roughly
10× cheaper.

### Ledger-kind mapping

The validator diffs the ingress-tap body against the upstream body and
classifies each difference. This lever produces exactly one:

| This kit's lever | Ledger kind |
|---|---|
| rewrites the request `model` | `route` |

Levers a miner could add, and the kinds they would produce (future work):

| Future lever | Ledger kind |
|---|---|
| condense a tool `description` (keep `name` + `input_schema`) | `tool_digest` |
| compact a completed-turn `tool_result` (keep `tool_use_id`) | `result_digest` / `context_compaction` |
| attach verbatim seeded-memory content | `memory_placement` |
| move or add a `cache_control` breakpoint | `cache_marks` |
| a side call with no tap counterpart (title, compaction, probe) | `side_call` |

### Hard constraints (all respected by this kit)

- **Determinism / prefix stability** — every transform is a pure function of
  the request bytes and static config, applied identically on every re-send.
- **No novel tokens / no contamination** — the lever never adds text upstream;
  seeded memory (including the canary) is never read or forwarded.
- **Catalog-only models** — the reroute target must be a route id in the
  frozen task catalog; the relay refuses anything else and never falls back.
  This kit hardcodes no model, so you cannot accidentally name an off-catalog
  route.
- **Relay-only egress** — the HTTP client refuses redirects and only ever
  reaches the configured relay.

## Extending

Add your levers in [`src/reroute.rs`](src/reroute.rs) (transform the request
before it is forwarded in `src/server.rs`). Wire seeded memory from
`src/seed.rs` into a `memory_placement` lever by attaching a relevant record's
**verbatim** content. Keep every transform deterministic and re-send-stable,
and check the per-arm ledger to see which levers actually paid.

Local ledger parity (matching the validator's classification byte-for-byte)
and the shared conformance vectors live in the separate
`RouterContractVersion` registry / `packages/dittobench-router-contract` epic
(not part of this kit).

## Validation

```bash
cargo fmt --check
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo test --locked --all-targets --all-features
```

`cargo test` covers the health advertisement shape, the archetype detector,
the surgical model splice, relay URL joining and ticket redaction, and the
seed store.
