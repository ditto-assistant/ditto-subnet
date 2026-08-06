# DittoBench scoring engine

DittoBench is the benchmark for Bittensor SN118, the Ditto subnet. This repo is
the engine that runs it: per submission it generates a fresh anti-cheat dataset
(procedural tool cases and a procedural persona memory haystack), runs a miner's
harness over every case, and scores the result deterministically.

The same binary runs in two places:

- On-chain, each subnet validator runs it to score miner submissions on its own
  hardware. See [What a validator does](#what-a-validator-does).
- Off-chain, the team hosts it as a keyless practice endpoint so miners can
  iterate without TAO or the chain. See [Off-chain practice](#off-chain-practice).

Generation is non-LLM and scoring is judge-free: a score is a pure function of
(dataset, transcript), graded by the deterministic checker in the public
[`dittobench-datagen`](https://github.com/ditto-assistant/dittobench-datagen)
module. Anyone can reproduce a composite from the dataset seed and the
transcript, so there is no central scorer to trust.

## What a validator does

A subnet validator scores miner submissions and sets on-chain weights from the
results. This repo is the scoring half of that job. Per submission it:

1. Loads the exact language-neutral OCI image built by the trusted screener.
   The source tarball is unpacked only for bounded, cross-language anti-copy
   fingerprinting; V7/V8 validator tickets never rebuild miner source.
2. Generates a fresh randomized DittoBench dataset, so no two evaluations match
   and a memorized lookup table cannot score.
3. Seeds the harness, runs every tool and memory case, and grades each case
   deterministically.
4. Returns a `ScoreReport`: the composite, the per-case breakdown, and the
   anti-copy sketch the platform's duplicate gate consumes.

The validator worker in
[`ditto-subnet`](https://github.com/ditto-assistant/ditto-subnet) drives the
engine end to end: it leases a scoring ticket from the platform, runs this engine
on a Docker-capable host, submits the signed score to the public ledger, and
recomputes the deterministic weights it sets on-chain. Because scoring is
judge-free and reproducible from the seed, any validator or third party can
re-derive a composite independently; no validator has to trust another's number.

Loading an image needs a Docker daemon, so a validator runs the engine on a VM
or similar host, not on a daemon-less platform. Point it at a dedicated
rootless daemon and require that boundary before accepting work:

```sh
export DOCKER_HOST=unix:///run/ditto-validator-docker/docker.sock
export DITTOBENCH_REQUIRE_ROOTLESS_DOCKER=1
go run ./cmd/dittobench-api   # listens on :8000 (or $PORT; -port to override)
```

The validator image adds the Docker CLI and Git to the same statically linked
API binary. If it is itself containerized, mount only the dedicated rootless
socket and set the same policy flag; do not mount `/var/run/docker.sock`:

```sh
docker build --target sandbox -t dittobench-api:sandbox .
docker run --rm \
  --user 65532:65532 \
  --group-add "$(stat -c '%g' /run/ditto-validator-docker/docker.sock)" \
  -v /run/ditto-validator-docker/docker.sock:/run/docker.sock \
  -e DOCKER_HOST=unix:///run/docker.sock \
  -e DITTOBENCH_REQUIRE_ROOTLESS_DOCKER=1 \
  -p 127.0.0.1:8000:8000 \
  dittobench-api:sandbox
```

The image runs the API as a non-root UID. The supplemental group must match the
Docker socket's group on the host; in Compose, use `group_add` with that numeric
GID. Docker Desktop users may need the socket GID exposed inside its VM instead
of the macOS host's value.

> **Security:** a rootful Docker socket is host-root-equivalent control even
> when the API process is non-root. The rootless daemon must run as a separate,
> empty host identity with no validator credentials. V7/V8 images are built by
> screeners without credentials, then run read-only with bounded tmpfs, memory,
> CPU, PIDs, file descriptors, IPC, logs, dropped capabilities, and
> no-new-privileges. Rootless limits a daemon/container escape to that empty
> identity; it does not make a shared host safe.

The binary is self-contained: dataset generation, execution, and grading run
locally with no external services. For how it slots into the worker, see the
subnet's
[`VALIDATOR-ONBOARDING.md`](https://github.com/ditto-assistant/ditto-subnet/blob/main/docs/VALIDATOR-ONBOARDING.md).

## Off-chain practice

The team hosts the same engine as a practice endpoint so miners can iterate
against a fresh dataset without the chain:

```
https://dittobench-api-22790208601.us-central1.run.app
```

Practice covers tool-calling correctness, memory recall, and tool efficiency. It
does not build images (the hosted instance has no Docker daemon), so to practice
you expose a reachable harness and submit its URL. No API key is needed; your
harness brings whatever model access it uses. Every request rotates the dataset
seed, so you cannot overfit to the practice set. The authoritative evaluation is
the on-chain validator run.

| Dimension              | Off-chain practice | On-chain validator |
| ---------------------- | ------------------ | ------------------ |
| Tool-calling accuracy  | yes                | yes                |
| Tool efficiency        | yes (observed)     | yes                |
| Latency (measured)     | yes (reported)     | yes                |
| Memory / embeddings    | yes (`run_size`)   | yes                |
| Fresh anti-cheat data  | yes                | yes                |
| Harness image execution | no (use `harness_url`) | yes            |
| TAO / chain            | no                 | yes                |

## How it fits with the starter kit

The companion monorepo directory
[`miners/dittobench-starter-kit`](../../miners/dittobench-starter-kit)
defines the miner harness: an HTTP server exposing `POST /run` (`RunRequest` to
`RunResponse`), `POST /seed` (load a fresh memory haystack), and `GET /health`.
You build your harness there, run it, expose it, then point this engine at its
URL.

```
miner harness (starter-kit)              scoring engine (this repo)
  POST /seed <───────────────────────────  install a fresh memory haystack
  POST /run  <───────────────────────────  run a fresh anti-cheat dataset
  GET  /health                              health check + score
```

## Control-plane authentication

The API port serves the validator's operator control plane. Every route on it
except `GET /health` requires a credential:

```
Authorization: Bearer $DITTOBENCH_BROKER_CONTROL_TOKEN
```

The credential is the same bearer the validator already presents on the
inference-session routes, so there is one secret per host rather than a second
scheme. `DITTOBENCH_CONTROL_TOKEN` overrides it if a deployment wants the
control-plane secret separated from the inference-broker one.

`GET /health` is the only route served without a credential. It returns a
constant `{"status":"ok"}` and reads nothing off the server, which is what makes
it safe to leave open for a container healthcheck. Any route added to the mux
later is protected by default — the public set is an allowlist, and a test
fails the build if a new route lands outside it unclassified.

### Rollout

`DITTOBENCH_CONTROL_AUTH_MODE` selects the posture:

| Mode | Behavior |
|---|---|
| `shadow` (default) | Checks the credential, logs what enforcement *would* reject, serves the request anyway. |
| `enforce` | Rejects with `401` when the credential is absent, malformed, or unrecognized. |

Shadow is the default so upgrading this service alone changes no behavior.
Before switching a deployment to `enforce`, watch for
`control-plane auth (shadow, would reject)` lines and confirm they have stopped
— those are the calls that will start failing. Under `enforce` the process
refuses to start without a credential rather than 401ing every caller, and the
published Compose default token is treated as unset, so enforcement requires a
real per-host secret.

## Endpoints

### `GET /health`
```sh
curl localhost:8000/health
# {"status":"ok"}
```

### `GET /v1/dataset?n=&seed=`
Pull a fresh randomized dataset to practice against. `seed` is random unless
pinned (pinning only reproduces a specific set); `n` defaults to 30.
```sh
curl 'localhost:8000/v1/dataset?n=10'
```

### Validator capability negotiation

`GET /v1/capabilities` is a read-only validator-control-plane endpoint that
reports only public release identity and supported protocol numbers. It needs no
operator secret: a shared bearer token would not authenticate the scorer because
the scorer itself would possess it. Production Compose does not publish the
scorer port on the host. The validator instead binds the response to the
immutable signed stack descriptor supplied by Compose:

```json
{
  "software_version": "0.10.0",
  "source_revision": "0123456789abcdef0123456789abcdef01234567",
  "source_revision_origin": "binary",
  "source_revision_mismatch": false,
  "supported_bench_versions": [2, 3]
}
```

Release identity is **derived from the compiled binary**, not asserted by the
environment. The image build links it in:

```sh
docker build --target sandbox \
  --build-arg DITTOBENCH_SOURCE_SHA=<40-hex commit> \
  --build-arg DITTOBENCH_SOFTWARE_VERSION=<release> .
```

`DITTOBENCH_SOFTWARE_VERSION` / `DITTOBENCH_SOURCE_SHA` remain as a **fallback**
used only when the image embedded nothing — they keep pre-existing images
working, but an environment variable can only ever state what an operator
*believes* is running. Recreating a container applies a new variable while
reusing the cached image, so the scorer would advertise a revision whose code it
does not contain. Two additive fields let a consumer tell the cases apart:

| field | meaning |
| --- | --- |
| `source_revision_origin` | `"binary"` (proven, compiled in) or `"env"` (asserted). Absent on scorers older than this field. |
| `source_revision_mismatch` | `true` when the binary and the environment named different revisions. The binary-derived value is still reported; treat the deployment as stale. |

`software_version_origin` reports the same distinction for the release string. A
mismatch never blocks startup — it is surfaced loudly in the log and in this
response so the subnet can degrade the validator instead of scoring blind.

Ask a container what it actually is, without starting it:

```sh
docker run --rm <image> version          # human-readable
docker run --rm <image> version -json    # machine-readable
```

It prints the embedded and asserted revisions, which one won, whether they
disagree, the release version, the supported bench versions, and the v7
calibration manifest digest with whether v7 is advertised.

The endpoint fails closed with 503 when the winning identity is absent or
malformed (including a deliberately stamped-but-malformed build). Validators
also fall back to v2 for an older scorer (404), an unreachable endpoint, a
malformed response, or any descriptor identity mismatch. This makes scorer and
validator upgrades order-independent without an `.env` cutover.

New validators send canonical work to `POST /v2/score`, where
`bench_version` is required and must be `2` or `3`. The accepted response and
every polled job echo `bench_version`; a completed report also echoes it at
`report.details.bench_version`. Validators must reject disagreement at any
layer. `POST /v1/score` remains available during the mixed-fleet migration and
maps an omitted version to the exact historical v2 path. `/v1/submit` retains
the same omission rule for public practice compatibility.

### `GET /v1/catalog`
The Ditto tool catalog the harness receives on every `/run`.
```sh
curl localhost:8000/v1/catalog
```

### `POST /v1/submit`
Generate a fresh dataset (rotating seed), run the harness over it, score, store,
and return. Three modes:

**Direct**: you run your own harness and the engine scores it (synchronous):
```sh
curl -X POST localhost:8000/v1/submit \
  -H 'Content-Type: application/json' \
  -d '{"harness_url":"http://localhost:9000","n":30}'
# {"run_id":"...","status":"done","composite":0.93,"tool_mean":0.93,"median_ms":42,"n":30,"seed":...}
```

**Sandbox**: the engine builds your submission in Docker and runs it, matching
the on-chain path (asynchronous; the build is slow). Returns `202` and a
`run_id`; poll `GET /v1/runs/{id}`. The container reaches only the locked gateway;
any model or provider key in `env` is dropped, so you cannot route around the
locked model:
```sh
curl -X POST localhost:8000/v1/submit \
  -H 'Content-Type: application/json' \
  -d '{"git_url":"https://github.com/<you>/<harness>","git_ref":"main","n":30}'
# {"run_id":"...","status":"queued","poll":"/v1/runs/..."}
```

For a monorepo, `git_subdir` selects the repository-relative Docker context.
Absolute paths, parent traversal, non-directories, and symlink escapes are
rejected after clone:

```json
{
  "git_url": "https://github.com/ditto-assistant/ditto-subnet",
  "git_ref": "main",
  "git_subdir": "miners/dittobench-starter-kit",
  "run_size": "small"
}
```

**Full pipeline (`run_size`)**: the complete SN118 evaluation. Generate a fresh
anti-cheat dataset, push the haystack to the harness's `POST /seed`, run every
tool and memory case, grade deterministically, and aggregate. No key needed.
Target a reachable `harness_url`, or `git_url` for the Docker-build path (local
or on-chain only). Asynchronous; returns `202` and a `run_id`. Poll
`GET /v1/runs/{id}` for `queued`, `building`, `generating`, `seeding`,
`running`, a transient `waiting_for_relay` recovery pause, `scoring`, then
`done` or `failed`, with live `progress` and
`partial` per-case scores.

```sh
curl -X POST https://dittobench-api-22790208601.us-central1.run.app/v1/submit \
  -H 'Content-Type: application/json' \
  -d '{"harness_url":"https://<your-harness>","run_size":"small"}'
  # small | medium | full ; "seed":N pins the dataset
# {"run_id":"...","status":"queued","poll":"/v1/runs/..."}
```

| run_size | tool cases | memory cases | seeding waves | raw-pairs frac | isolation |
| -------- | ---------- | ------------ | ------------- | -------------- | --------- |
| small    | 6          | 6            | 1             | 0              | 0         |
| medium   | 40         | 52           | 4             | 0.45           | 4         |
| full     | 84         | 185          | 5             | 0.5            | 10        |

Config for the `run_size` path (all optional):

| Source                  | Default            | Purpose                                              |
| ----------------------- | ------------------ | ---------------------------------------------------- |
| `DITTOBENCH_PLATFORM_INFERENCE_PROXY_URL` env | required in production | exact platform chat endpoint accepted during ticket activation |
| `DITTOBENCH_EMBEDDING_UPSTREAM_URL` env | required in production | exact platform embedding endpoint used by the source-bound broker |
| `DITTOBENCH_BROKER_PORT` env | `11436` | harness-only local listener for ticket-scoped chat and embeddings |

The locked models are scored against every harness; their identities are frozen
in code and in the platform-issued ticket, not selected by validator or miner
environment.

### Harness images (on-chain only)

The active V7/V8 path loads the content-addressed image produced by the trusted
screener, then runs its HTTP contract. Rust, Python, TypeScript/JavaScript, Go,
Java, C/C++, C#, Ruby, or any other language is valid: the runtime contract is
only a root `Dockerfile` and the documented HTTP endpoints. Local development
may still build `git_url`/`tarball_url` source, but no GitHub/provider credential
is mounted into that untrusted build. The hosted practice instance has no Docker
daemon; submit a reachable `harness_url` there instead.

### `GET /v1/runs/{id}`
Fetch the job: `status`, `mode`, and (when `done`) the full `ScoreReport` with
the per-case breakdown, plus `transcript_sha256` — the digest of the run's
canonical transcript artifact. Returns `404` if unknown.
```sh
curl localhost:8000/v1/runs/<run_id>
```

Failed sandbox runs may also include a bounded `failure` object. Only
validator-owned resource exhaustion is marked `kind=validator_infrastructure`
with `retryable=true`; ordinary non-zero miner exits remain non-retryable. Its
diagnostics are limited to Docker state, exit code, OOM/cgroup counters, and
aggregate `/tmp` usage. Container IDs, environment, output, paths, and source
are never returned.

### `GET /v1/runs/{id}/transcript`
The run's canonical transcript artifact: every graded case's exact inputs (the
`RunResponse` as graded plus the validator-observed trajectory), sorted by
case id so the bytes — and therefore `transcript_sha256` — are deterministic.
Together with the seed-regenerated dataset this is everything a third party
needs to re-run the public grader and reproduce the score offline. Returns
`404` until the run finishes.
```sh
curl localhost:8000/v1/runs/<run_id>/transcript
```

## Scoring

Scoring is deterministic: a pure function of (dataset, transcript), reproducible
by anyone from the public `dittobench-datagen` module. See
`docs/judge-determinism.md` for the grading rules.

Tool cases: deterministic trajectory and argument accuracy
(0.4 name-F1 + 0.4 arg-F1 + 0.2 order and extra-call discipline), scored on the
validator-observed trajectory. An observable case the harness did not execute
through the tool endpoint is capped at 0.5 in practice and scores 0 on the
scored path (observed execution is mandatory there). Result-usage cases also
require the served needle value in the answer; an answer carrying the served
decoy instead zeroes the usage half.

Memory cases (`run_size` only): per-`answer_kind` deterministic grading (value,
number, list, ordered list, duration, reversal, decline) with distractor
zeroing, over the response's answer slot with a prose fallback.

Composite: the mean tool score in direct mode; in the full pipeline
`0.5·tool_mean + 0.5·memory_mean`, then multiplied by three bounded integrity
factors (each at most 1): observed tool-efficiency (penalizes overshooting the
expected call budget on correctly-answered cases the validator watched execute
through the tool endpoint), canary-integrity (a canary leak multiplies by 0.5 and
compounds; an honest miss carries no composite penalty, since it is already
reflected in the case's own accuracy), and metamorphic-consistency (penalizes answering
paraphrased twins of the same fact inconsistently). tool_mean, memory_mean, and
per_category stay pure accuracy; the factors touch only the composite.

Latency is measured by the validator (the `/run` round trip, never
self-reported) and reported as `median_ms`. It is advisory telemetry: accuracy
and efficiency drive the score, not wall-clock, which on a remote harness mostly
reflects network and hardware.

## Anti-overfit

Every `/v1/submit` (and `/v1/dataset` without a pinned seed) rotates the seed, so
the dataset is fresh each time. Memorizing answers does not help; only a
genuinely correct harness scores well. The authoritative, larger evaluation lives
on the on-chain validator.

## Anti-copy

Duplicate detection is a platform-side gate, not part of any score computed here.
This engine forwards two inputs to it: the `structural_fingerprint` sketch in the
`ScoreReport` and the observed tool-call trajectory (see `PROTOCOL.md`, Anti-copy
signals). The gate holds a suspected copy for review, with first-seen protecting
the original author, and requires agreement across independent signals before
flagging, so independent convergence on the shared reference harness is not
penalized.

## Public endpoint hardening

The hosted practice instance is public, unauthenticated, and dials
caller-supplied harness URLs, so it guards against abuse:

- SSRF: `harness_url` must be an `http(s)` URL resolving to a public address.
  Loopback, RFC1918 private, link-local (including the `169.254.169.254` metadata
  IP), CGNAT, and multicast are rejected up front (`internal/netguard`), and the
  outbound dialer re-checks the connected IP to defeat DNS rebinding.
- Rate limiting: a per-IP sliding window on `/v1/submit` (`internal/ratelimit`)
  plus a single active `run_size` job per scorer; both return `429`. A full
  miner container is capped at 3 GiB RAM, 2 CPUs, 512 PIDs, and a 512 MiB
  writable `/tmp`; the root filesystem remains read-only. Keeping concurrency
  is bounded separately so those limits cannot overcommit the validator host
  alongside Docker, Pylon, and the worker. A request-body cap rejects oversized
  payloads.
- `DITTOBENCH_ALLOW_PRIVATE_HARNESS`: set truthy for local dev or the Docker
  sandbox (loopback containers) to relax the SSRF guard. Leave it unset in
  production; the guard is on by default.
- Screened-image metadata pins integrity but is not an authentication token.
  The prebuilt-image path is therefore rejected on the public practice API and
  is only enabled on validator-owned sandbox deployments by the narrow
  `DITTOBENCH_ALLOW_SCREENED_IMAGES=1` opt-in. The validator must keep that API
  private. `DITTOBENCH_ALLOW_PRIVATE_HARNESS` remains separate and is only
  needed when local source/image URLs themselves resolve to private addresses.
  Imported archive and local runner tags are removed after each run so validator
  disks do not accumulate submission images.
- Benchmark v7 and v8 require the screener-built, digest- and image-ID-bound
  archive; the source-build fallback is retired. That contract governs
  validator-side builds, so it applies to `git_url` and `tarball_url` on every
  deployment. A `harness_url` practice submission builds nothing — the miner
  runs their own harness and this engine only drives and scores it — so it is
  outside the contract and remains the supported v8 practice path. The canonical
  validator path (`POST /v2/score`) still requires the screened image for every
  source kind, `harness_url` included.
- Miner containers run as an unprivileged UID with a read-only root filesystem,
  an ephemeral no-exec `/tmp`, all capabilities dropped, no-new-privileges,
  bounded CPU/memory/PIDs/file descriptors, and request-scoped cleanup.
  `DITTOBENCH_DB=/tmp/dittobench.db` is enforced after caller/image environment
  values so a screened image cannot depend on writing under `/app`. A container
  that exits before health is reported immediately instead of consuming the
  full health timeout.
  Set
  `DITTOBENCH_SANDBOX_SECCOMP_PROFILE` and
  `DITTOBENCH_SANDBOX_APPARMOR_PROFILE` to reviewed host profiles where those
  Linux security modules are available.

## See also

- `PROTOCOL.md`: the shared wire contract, including ticket-scoped platform
  inference and hosted embedding identity.
- `docs/judge-determinism.md`: why scoring is judge-free and how each case kind
  is graded.
- `docs/token-efficiency-v7.md`: the current quality-only scorer contract and
  platform-owned dynamic relative efficiency policy.
- `docs/hermes-benchmark/README.md`: the reproducible Hermes Agent adapter and
  sanitized full-v6 OpenRouter measurement.
- `docs/openclaw-benchmark/README.md`: the native-memory OpenClaw adapter and
  sanitized full-v6 OpenRouter measurement.

## License

MIT. See [`LICENSE`](LICENSE).
