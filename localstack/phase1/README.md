# Phase-1 real-stack localstack (scored v12, gate FIRING)

Completes a **scored `bench_version=12`** run end to end with the v12 **causal
model-dependence gate + counterfactual firing**, using a **real Platform grant**
(model-relay `/exchange`, sr25519) and **real OpenRouter inference**. Only chain
and screening are mocked.

## Why a container

The dittobench-api broker forwards chat to an **https** platform proxy with an
Ed25519 proof, over Go's default-trust client. Go honors `SSL_CERT_FILE` on
**Linux** but not macOS, so the broker runs in a Linux container that trusts the
dev terminator CA via `SSL_CERT_FILE=/ca.pem`. The harness is co-located in that
container so the broker's loopback source-binding (`loopbackHarnessSourceIP`
requires `127.0.0.1`) and the harness→broker inference calls both see `127.0.0.1`.

## Topology

```
host:   postgres :5442 (docker)   model-relay :8082  ──►  OpenRouter (gpt-oss-20b)
                                    ▲
                        tls-terminator :8443 (dev cert, SAN host.docker.internal)
                                    ▲  https + Ed25519 proof
container ds-broker (Linux):  dittobench-api broker :8010 (published) / :11436 (internal)
                              + harness :9000   (SSL_CERT_FILE=/ca.pem trusts the CA)
```

## Run

```bash
./localstack/phase1/up.sh                 # HARNESS=model (default) | HARNESS=refharness
                                          # CONSTANT_ANSWER="..." for the model-independent variant
./localstack/phase1/handshake.sh          # prepare -> exchange(sr25519) -> activate -> scored /v1/submit -> poll
./localstack/phase1/down.sh               # stop container + host relay/terminator (FULL=1 also stops postgres)
```

`handshake.sh` is self-contained: it re-seeds the grant (pending, fresh deadline)
at the start and DELETEs the session at the end, so you can run it repeatedly
(e.g. per agent) without restarting the broker.

## The handshake (what `handshake.sh` does)

1. **prepare** — `POST :8010/v1/inference/session` (Bearer `localdev`) → `session_id`,
   `activation_secret`, `broker_public_key`.
2. **exchange** — sign `validator-inference:v1:{hotkey}:{grant_id}:{bpk}:{nonce}:{requested_at}`
   with the validator sr25519 key (`exchange_sign.py`, bittensor), then
   `POST :8443/api/v1/inference/exchange` (`X-Validator-Hotkey`) → real grant
   (`bearer`, `generation`, `proxy_url`, `provider/profile_revision/model`, budgets).
3. **activate** — `POST :8010/v1/inference/session/{id}/activate` with the grant +
   identity quad (`grant_id`, `agent_id`, `slot_id=slot-0`, `ticket_deadline`).
4. **submit** — scored `POST :8010/v1/submit` with `dataset_sha256`, `tarball_sha256`
   (any 64-hex; the direct path has no real tarball), `inference_session_id`, and the
   identity quad. Poll `/v1/runs/{id}`.

## Seed (chain + screening mocked)

`up.sh` seeds `agents → validator_tickets(issued) → inference_grants(pending)` for
the sr25519 hotkey, bench 12, `slot-0`, `route_profile=openrouter-route-6a097486af3c178d-v1`,
`allowed_models=["openai/gpt-oss-20b"]`. `DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR=1`
+ `SUBTENSOR_NETWORK=local` bypass the chain permit; a dummy `PYLON_OPEN_ACCESS_TOKEN`
satisfies model-relay boot.

## Validations captured (localstack/.run/phase1/report-*.json)

- **model-using harness** → `model_use: passed`, `model_dependence: passed`
  (administered=30, eligible=30, **dependent=30**, slice_complete). Counterfactual
  administered on all 30 cases. Composite gated to 0 only by `authoritative_tool`
  (the minimal harness runs no tools).
- **model-independent harness** (`CONSTANT_ANSWER`) → `model_use: passed`,
  `model_dependence: below_threshold` (**dependent=0/30**) → composite **0.00**. The
  gate fail-closes for an agent whose answers don't depend on the model.
- **refharness** (zero inference) → terminal `model_inference_required`: it cannot
  route the post-run model-route probe, so it fails at the model-use gate before
  `model_dependence` (it never reaches the counterfactual).

## Files

- `up.sh` / `down.sh` — bring the stack up/down.
- `handshake.sh` — the full handshake + scored submit + report.
- `exchange_sign.py` — sr25519 exchange signer (bittensor).
- `seed.sql` — the seeded grant path (written by `up.sh`).
- Binaries/logs/reports under `localstack/.run/phase1/` (gitignored).
- `services/dittobench-api/cmd/localstack-tlsterm` — dev TLS terminator.
- `services/dittobench-api/cmd/localstack-modelharness` — minimal model-using harness.
