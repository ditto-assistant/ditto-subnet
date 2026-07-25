# Validator operations (SN118)

A validator leases miner submissions, scores them in an isolated local sandbox,
publishes signed results, and sets weights on Finney. Production runs the
validator from an immutable GHCR digest with the repository's cooperative
updater; building from source is a fallback when the release channel is
unavailable.

## Contents

- [What runs](#what-runs)
- [Requirements](#requirements)
- [First deployment](#first-deployment)
- [Verify health](#verify-health)
- [Upgrade and operate](#upgrade-and-operate)
- [Automatic full-stack updates (recommended)](#automatic-full-stack-updates-recommended)
- [How scoring and weights work](#how-scoring-and-weights-work)
- [Optional observability](#optional-observability)
- [Development](#development)

## What runs

The root Docker Compose stack starts six services:

| Service | Purpose |
| --- | --- |
| `ditto-subnet` | Leases work, signs scores, and computes weights. |
| `dittobench-api` | Scores submissions. |
| `sandbox-docker` | Isolated nested Docker daemon that runs miner containers. |
| `model-relay` | Reaches the locked model on the selected provider without exposing its key. |
| `ollama` | Serves the embedding model used for memory scoring. |
| `pylon` | Submits weights with the validator wallet. |

During the v7 transition, `ollama` remains required for backwards-compatible
v2-v6 work. A v7 ticket instead routes the same locked 768-dimensional embed
operation through `dittobench-api` to the platform-owned OpenRouter profile;
the functional hosted-vector preflight runs after ticket exchange and before
the miner container starts. No OpenRouter credential is installed on the
validator.

The validator is stateless: the queue and score ledger live on the platform,
and Pylon keeps in-flight weight state in a named volume. The platform screens
every submission before it reaches validators and ships a verified pre-built
Docker image with it, so your host normally does not compile miner code.

Every miner container runs with strict CPU, memory, PID, capability, seccomp,
and egress limits. The stack advertises four full-run slots and the platform
decides how many of them actually receive tickets, so the concurrency your host
runs is an operator setting on the platform (default two), not a value you edit
here. Lower `VALIDATOR_BENCHMARK_CAPACITY` only if your host is below the
documented 16 GB RAM / 80 GB disk; each concurrent run adds a miner sandbox with
a 3 GiB memory cap plus its own image and writable layer.

## Requirements

- Linux x86-64 with at least 4 vCPU, 16 GB RAM, and 80 GB free disk.
- Docker Engine, Buildx, and the Docker Compose plugin v2 or newer, including
  v5. Docker must start at boot.
- Git and `flock` from util-linux.
- A local Bittensor wallet whose hotkey is registered on Finney SN118 and has a
  validator permit.
- During the bounded v6 transition, the existing frozen-relay provider key and
  outbound provider access. V7 does not consume this key, and a later cleanup
  release removes both after activation and all v6 leases drain.
- Outbound access to Finney, the Ditto platform, and GHCR (anonymous pull of the public
  `ghcr.io/ditto-assistant/ditto-subnet-validator` package).

Python and `uv` are only required for development.

## First deployment

Clone the repository and create the one environment file used by Compose:

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
cp .env.example .env
openssl rand -base64 32
```

Put the generated value in `PYLON_TOKEN`, then fill these values in `.env`:

| Env | Value |
| --- | --- |
| `VALIDATOR_HOTKEY` | Public SS58 address of the permitted validator hotkey. |
| `VALIDATOR_WALLET_NAME` | Coldkey directory under `~/.bittensor/wallets`. |
| `VALIDATOR_WALLET_HOTKEY` | Hotkey file inside that wallet. |
| `PYLON_TOKEN` | Random token generated above. |
| `VALIDATOR_BENCHMARK_CAPACITY` | Full-run slots this host advertises. Leave unset to take the compose default of `4`; the platform caps how many are used. |
| `RELAY_PROVIDER` / `RELAY_API_KEY` | Existing frozen v6 route only; retain during transition. |
| `DITTOBENCH_REQUIRE_TICKET_INFERENCE` | Leave `false` until v6 drains; v7 is independently fail-closed. |
| `VALIDATOR_INFERENCE_PROXY_REQUIRED` | Leave `false` until v6 drains; v7 is independently fail-closed. |

The example selects Finney, SN118, and the production platform. For local
testing, change both the platform and chain settings in a separate `.env`.

The wallet remains on the host and is mounted read-only. The loaded hotkey must
exactly match `VALIDATOR_HOTKEY`. Never put a mnemonic in `.env`, never commit
`.env`, and never run two validator stacks with the same hotkey.

Resolve the release channel to the exact digest that will run:

```sh
IMAGE=ghcr.io/ditto-assistant/ditto-subnet-validator
docker pull "$IMAGE:compat-2"
DIGEST="$(
  docker image inspect \
    --format '{{ range .RepoDigests }}{{ println . }}{{ end }}' \
    "$IMAGE:compat-2" |
    awk -v prefix="$IMAGE@" 'index($0, prefix) == 1 { print; exit }'
)"
test -n "$DIGEST"
printf '%s\n' "$DIGEST"
```

Stop if the pull or digest check fails. Do not substitute a mutable tag or an
unpromoted image; use the [source-build fallback](#development) until the
channel is available.

Start the five sidecars, then start only the digest-pinned validator:

```sh
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build --wait --wait-timeout 180 \
  pylon sandbox-docker model-relay ollama dittobench-api
DITTO_SUBNET_IMAGE="$DIGEST" \
  ./scripts/validator-compose.sh up -d --no-deps --no-build --pull never \
  ditto-subnet
./scripts/validator-compose.sh logs --since 10m ditto-subnet
```

After the validator reports a fresh platform-accepted heartbeat, enter managed
mode with the supervised stack migration in
[Automatic full-stack updates (recommended)](#automatic-full-stack-updates-recommended).
Perform it during a window with no live ticket; never interrupt a running
benchmark to enter managed mode.

## Verify health

```sh
./scripts/validator-compose.sh ps
./scripts/validator-compose.sh logs --since 10m ditto-subnet
curl -fsS https://platform-api.heyditto.ai/health
./scripts/validator-stack-auto-update.sh status
```

All six services should be `Up`; `ollama`, `sandbox-docker`, and
`dittobench-api` should be healthy. An idle validator may log
`scoring sweep complete: 0 agent(s)`. That is normal when the queue is empty.

Production acceptance also requires:

- platform health reports `db: ok` and `chain: ok`;
- the hotkey has a validator permit on SN118;
- sweeps complete without recurring platform, scorer, or Pylon errors;
- the on-chain last-update block advances after a weight submission; and
- the public validators endpoint lists the hotkey online.

## Upgrade and operate

In managed mode, the stack updater performs every upgrade — the validator and
all five sidecars are replaced together as one transaction. Do not use direct
`docker compose`, a second supervisor, or manual restarts; those paths can
replace a reviewed digest or interrupt leased work. The host launcher scripts
are outside the signed bundle, so keep the repository checkout on the reviewed
release with `git pull --ff-only`.

Useful commands:

```sh
./scripts/validator-compose.sh ps
./scripts/validator-compose.sh logs -f ditto-subnet
./scripts/validator-compose.sh logs --since 10m sandbox-docker
./scripts/validator-compose.sh logs --since 10m dittobench-api
./scripts/validator-compose.sh logs --since 10m pylon
./scripts/validator-stack-auto-update.sh status
```

### Stale scorer image

`docker compose up` reuses an image whenever one already exists. When the
reviewed `dittobench-api` pin in `docker-compose.yml` moves, a plain `up`
therefore recreates the scorer container with the **new** pin's environment on
top of the **old** binary. The scorer then reports a source revision it was
never built from, the validator's identity check passes, and the validator
silently serves an outdated benchmark set — it stops advertising the active
benchmark version, so submissions needing k=3 quorum hang instead of failing.

Two things prevent that now.

1. `scripts/validator-compose.sh` records the revision it last built. When a
   command that can start containers runs against a **different** pin, it forces
   `docker compose build --pull dittobench-api model-relay` and replaces those
   two containers before running your command. An unchanged pin does nothing, so
   restarts stay fast and work without network access. A failed rebuild stops
   the command instead of starting the previous image.
2. The validator refuses to call a scorer verified unless its identity is
   proven by the running binary. `dittobench-api` links its revision in at build
   time and reports where the value came from (`source_revision_origin`), plus
   whether the binary and the environment disagreed
   (`source_revision_mismatch`). With
   `VALIDATOR_SCORER_REQUIRE_BINARY_PROVENANCE=1` — committed next to the pin,
   because only a pin whose scorer stamps itself can satisfy it — a revision
   that is merely asserted by the environment is treated as an unrebuilt image.
   That is exactly the evidence that lied during the incident.

A validator in that state logs `scorer_image_stale`, reports
`capabilities.scorer_benchmarks.status = identity_mismatch` and
`stack_health.dittobench_api.health = identity_mismatch`, and advertises
benchmark v2 only. It keeps heartbeating and never crash-loops, so the fault is
visible on the dashboard rather than hidden behind a green validator.

On a managed stack the next stack update replaces the scorer from the signed
descriptor, so no manual rebuild is needed. On a source-built install, rebuild
the scorer from the pin during a supervised window with no live lease. The
Compose wrapper detects the moved pin and rebuilds before it runs your command:

```sh
git pull --ff-only
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build --no-deps --wait \
  --wait-timeout 180 dittobench-api model-relay
./scripts/validator-compose.sh logs --since 10m ditto-subnet
```

The validator logs `scorer identity verified` once the rebuilt scorer answers.

### Scorer liveness

A running scorer container is not a serving scorer. From heartbeat protocol 15
the validator reports what its `/v1/capabilities` probe actually did, alongside
the conclusion it drew, under `capabilities.scorer_benchmarks.probe`:

| Field | Meaning |
| --- | --- |
| `outcome` | `served` (a document read in full), `served_degraded` (readable, part rejected), `http_error`, `unreadable` (200 with an unusable body), `timeout`, `connect_error`, `not_probed` |
| `observed_at` | when this probe ran |
| `http_status` | the status the scorer answered with, when it answered |
| `reason` | why a readable reply was still not fully usable |
| `last_served_at` | when the scorer last answered with a fully readable document |
| `consecutive_failures` | probes since the last fully readable document |

`last_served_at` and `consecutive_failures` are counted by the running validator
process and reset when it restarts, so a fresh process reports no history rather
than a history it cannot support.

This exists because the status alone cannot distinguish two very different
scorers. A sidecar that 404s `/v1/capabilities` and a genuine pre-capabilities
scorer both produce `status: legacy_v2` with every identity field null; the
probe shows `http_error` / `404` for the first. A scorer whose capability
document is only partly readable still produces `status: fresh_verified`; the
probe shows `served_degraded` and names the field that was rejected.

On the platform's fleet view a validator whose probe reports no usable answer
reads `health: critical` with `scorer_liveness: not_serving`. Its `admission`
is unchanged: the validator keeps taking work, which is why the condition needs
to be visible.

### Troubleshooting

- **GHCR pull fails:** confirm outbound access to `ghcr.io` and that `compat-2`
  exists. Do not guess a digest or fall back to a mutable tag.
- **No work is scored:** zero queued agents is normal. Otherwise inspect the
  validator, sandbox, scorer, relay, and Ollama health before restarting
  anything.
- **Runs fail with `tool_endpoint advertised but unreachable`:** one failure
  can be a non-compliant miner harness; recurring failures across different
  agents mean your sandbox networking (`host.docker.internal` routing) is
  broken — fix it before the reopened tickets expire. No zeroed score is
  signed either way.
- **Logs show `transcript publication failed`:** the accepted score already
  stands. Check `dittobench-api` health and platform reachability so future
  runs publish their transcripts.
- **The fleet view shows `Scorer down` / `scorer_liveness: not_serving`:** the
  validator's `/v1/capabilities` probe got no usable answer. Read
  `capabilities.scorer_benchmarks.probe`: `connect_error` means nothing is
  listening, `timeout` means the scorer is wedged, and an `http_error` with
  `404` means the container is up but never mounted the route (check that the
  `dittobench-api` image matches the pin rather than restarting the validator).
  See [Scorer liveness](#scorer-liveness).
- **Logs show `scorer_image_stale` or `scorer_revision_mismatch`:** the running
  scorer is not the pinned `dittobench-api` revision. See
  [Stale scorer image](#stale-scorer-image). Restarting the validator does not
  help — the scorer image itself must be rebuilt from the pin.
- **v7 tickets fail immediately with `reason=infrastructure`, scorer logs show
  `inference control plane unavailable` (401):** the scorer and the validator
  disagree about the inference control token. The scorer joins
  `sandbox-docker`'s network namespace while the validator stays on the Compose
  bridge, so the validator is never a loopback peer and must present
  `DITTOBENCH_BROKER_CONTROL_TOKEN` as a bearer. Both values come from one
  Compose anchor and have a working default, so this only appears when the two
  services run from different Compose revisions — recreate `dittobench-api`
  and `ditto-subnet` from the same checkout/release. If you set
  `DITTOBENCH_BROKER_CONTROL_TOKEN` yourself, both services must be recreated
  after changing it.
- **Updater reports a transaction:** keep the timer disabled, verify the
  validator and all sidecars, then use `recover`.
- **Host rebooted:** verify Docker is enabled and active, then check Compose
  and updater status. Do not add PM2 or another systemd service for the stack.
- **Disk use grows:** inspect `sandbox-docker`. Its nested daemon prunes unused
  benchmark data; do not run broad cleanup against the host Docker daemon.
- **Sandbox resource failure (`sandbox_oom`, `sandbox_tmpfs_exhausted`):**
  these are validator-infrastructure classifications, not miner verdicts; the
  worker stops claiming work and the ticket expires safely. Fix the resource
  issue, then ask the Ditto team for the audited single-agent retry; never
  bulk retry or alter accepted scores.

## Automatic full-stack updates (recommended)

Enable the managed stack updater unless you run your own update automation.
Patch releases ship often, and the platform routes work by advertised
compatibility, so a validator that lags the release channel falls out of
ticket routing; the updater keeps the complete immutable Compose stack current
as one transaction with automatic rollback. If
you maintain your own updater instead, that is fine — it must track the
`compat-2` channel promptly, pin exact digests, and drain the validator before
replacing services.

The migration preflight, transaction boundaries, rollback guarantees, and
trust policy are documented in
[FULL-STACK-UPDATES.md](FULL-STACK-UPDATES.md); read it before the first
cutover.

Update this checkout to the exact reviewed release, install Cosign from its
verified upstream release, and migrate. If this host ever ran the retired
validator-only updater, disable its timer first. `migrate` waits for the
validator to drain, installs all six exact services, and verifies a fresh
accepted heartbeat before recording the stack:

```sh
sudo systemctl disable --now ditto-validator-auto-update.timer 2>/dev/null || true
STACK=ghcr.io/ditto-assistant/ditto-subnet-stack
docker pull "$STACK:compat-2"
DIGEST="$(
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "$STACK:compat-2" |
  awk -v prefix="$STACK@" 'index($0, prefix) == 1 { print; exit }'
)"
test -n "$DIGEST"
./scripts/validator-stack-auto-update.sh migrate "$DIGEST"
./scripts/validator-stack-auto-update.sh status
```

If all six services already match the descriptor, `adopt "$DIGEST"` records
them without replacement. Both commands fail closed if the descriptor's
signature is not from this repository's release workflow.

Enable the timer only after migration or adoption succeeds:

```sh
if grep -q '^VALIDATOR_STACK_AUTO_UPDATE=' .env; then
  sed -i 's/^VALIDATOR_STACK_AUTO_UPDATE=.*/VALIDATOR_STACK_AUTO_UPDATE=true/' .env
else
  printf '\nVALIDATOR_STACK_AUTO_UPDATE=true\n' >>.env
fi
sudo DITTO_VALIDATOR_UPDATE_USER="$USER" \
  ./scripts/install-validator-stack-auto-update.sh
systemctl list-timers ditto-validator-stack-auto-update.timer
./scripts/validator-stack-auto-update.sh status
```

Compatible patch and minor releases then install automatically: the updater
drains the validator (an active benchmark finishes first), replaces all six
services as one transaction, and rolls the complete previous stack back if the
new one fails verification. Major or schema changes require supervised
migration.

To disable updates, inspect an interrupted run, or roll back manually:

```sh
sed -i 's/^VALIDATOR_STACK_AUTO_UPDATE=.*/VALIDATOR_STACK_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-stack-auto-update.timer
sudo systemctl stop ditto-validator-stack-auto-update.service
./scripts/validator-stack-auto-update.sh status
./scripts/validator-stack-auto-update.sh rollback   # manual rollback only
```

If everything is healthy but `status` shows `transaction_phase`, run
`./scripts/validator-stack-auto-update.sh recover` only after verifying that
resuming lease intake is safe.

## How scoring and weights work

The platform leases each submission to independent validators and finalizes the
median signed score. Each ticket pins the workload and deadline; expired work
reopens automatically, and every benchmark run must originate from a live
platform ticket. Each scored run starts with a reachability preflight that
requires the miner harness to call the mock tool endpoint; if the probe is
never observed the run fails and the ticket reopens — a zeroed report is never
signed. After the platform accepts a score, the worker publishes the run's
graded transcript for public verification.

The validator computes the deterministic weight vector from the public
finalized ledger, and Pylon handles UID resolution, commit-reveal, retries, and
the on-chain extrinsic on an independent cadence that honors the chain rate
limit and subnet tempo.

During a benchmark-version rollout, the platform selects one authoritative row
per agent: the desired-version median after 3/3, otherwise its active-version
fallback. The ledger can therefore intentionally contain both v2 and v3 rows.
Validators fold that full platform-selected pool; they must not apply a global
"maximum benchmark version" filter, because older validators ignore the
additive `bench_version` field and already fold the full pool. This preserves
identical weights across asynchronous validator upgrades.

## Optional observability

Add the shared `WANDB_API_KEY` supplied by Ditto to `.env`, or set
`WANDB_MODE=disabled`. Never commit the key.

The validator also sends a signed public heartbeat with its version, source
digest, phase, and coarse health; the platform uses it to route compatible
work. It does not send secrets, prompts, expected answers, model output, or
host identity.

Heartbeat protocol 10 adds authoritative bounded capacity: configured and
healthy slot ids, admission state, and privacy-safe progress for every active
benchmark. Active heartbeats refresh every 30 seconds, with changed aggregate
question counts eligible every 15 seconds. The stack advertises four slots by
default and the platform's operator cap decides how many receive tickets;
draining or paused validators advertise no healthy slots and receive no new
work.

### Per-component stack health

Heartbeat protocol 9 adds a signed health entry for each of the six Compose
components. Three ideas are reported separately and must not be conflated:

- **Configured identity** — what Compose intends to run (the pinned image
  digest / source revision already reported under `stack`). It proves intent,
  not the running artifact.
- **Observed identity** — what a live probe could independently verify (for
  example the scorer's `/v1/capabilities` revision). When nothing can be
  observed safely the field stays unset; the validator never copies the
  configured pin into an observed field.
- **Functional readiness** — whether the component answered a real request
  just now (`ready`, plus `model_ready` for the embedding model / model
  route), with its own `observed_at`, so a stale probe is distinguishable from
  a stale heartbeat.

Each component reports `healthy`, `degraded`, `unreachable`,
`identity_mismatch`, or `unknown`. Probes run from the validator over the
private Compose network — no Docker socket is mounted for telemetry — and are
bounded so a wedged sidecar never stalls the heartbeat. Optional env:

- `VALIDATOR_SANDBOX_DOCKER_PROBE_URL` / `VALIDATOR_MODEL_RELAY_PROBE_URL` —
  internal readiness endpoints. The relay probe is removed with the v6 cleanup.
- `VALIDATOR_PYLON_PROBE_URL` — defaults to `PYLON_URL`.
- `VALIDATOR_STACK_PROBE_TIMEOUT_SECONDS` (default 2) and
  `VALIDATOR_STACK_HEALTH_CACHE_SECONDS` (default 60).

Probe URLs are configuration only; the public payload carries health states,
timestamps, booleans, and verified identities — never URLs, hostnames,
container ids, or paths.

## Development

The source-build path is a fallback when the reviewed GHCR compatibility
channel is unavailable. It does not enter managed updater mode:

```sh
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

Upgrade a source-built validator only during a supervised window with no live
lease: `git pull --ff-only`, then the same three commands.

The wrapper builds `dittobench-api` from the exact reviewed commit pinned in
`docker-compose.yml` and refuses a checksum that is not in that repository's
`main` history. When the pin has moved since the last build it rebuilds and
replaces the scorer and relay containers first, so `up` can never start a cached
image against a newer pin — see [Stale scorer image](#stale-scorer-image).

For local code work outside Compose:

```sh
uv sync
make lint typecheck test
```

The worker entry point is `uv run python -m ditto.validator`.

### One emission position per coldkey

The platform ledger contains at most one generation for each coldkey captured
at upload-payment time. Different agent names and hotkeys owned by that coldkey
compete for the same position; the best fully eligible canonical score wins,
with first submission time and agent UUID as deterministic ties. The winning
row's hotkey remains the chain weight destination.

Validators do not query current coldkey ownership or perform a second collapse.
They fold the platform's identical, payment-time snapshot so hotkey rotation or
a later ownership change cannot make validators disagree. A held, banned, or
otherwise ineligible newer generation cannot shadow an older eligible winner.
