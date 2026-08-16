# Screener orchestrator

This process is the sole normal writer of screener capacity. It reads runnable
demand from Platform, acquires a fenced controller lease, and applies the
audited Backroom revision: Targon-first when a lane starts with `targon`, or
the old GCE-only path otherwise. A stored `['gcp', 'targon']` list is not a
working hybrid; first-provider wins, so that list disables Targon for the lane.
The GCE MIG stays as the safety path until Targon is validated. Both providers
may return to zero when the queue and active leases are empty.

Targon Rentals have three independently controlled jobs: credential-minimal
Kaniko builds, direct-image runtime health checks, and bounded read-only L1
source review. Hostile full-worker execution remains fail-closed behind an
expiring capability attestation because the nested RootlessKit probe failed.
GCE remains authoritative: it imports each verified miner archive and reruns
the isolated fake-gateway health/oracle and any elevated L2/L3 source review.

Provider credentials are accepted only through mode-0600 files. The operator
smoke wrapper streams `TARGON_API_KEY` directly from GCP Secret Manager to the
process over stdin; it never exports or prints the value.

## Provider trust boundary

Targon controls the rental runtime and can inspect every workload environment
variable, command, mounted file, process, and memory page. Workload env is not a
secret boundary. The controller therefore never sends the Targon API key or a
long-lived GCP credential to a rental. The only authorities injected during
bootstrap are a node-bound single-use registration capability and a 30-minute
GCP access token scoped to one Secret Manager resource; the worker consumes
them into mode-0600 files and removes them from its environment immediately.

Values supplied through `--targon-worker-env-file` must be treated as public to
the provider. Long-lived API keys, service-account JSON, mnemonics, database
URLs, and controller credentials are prohibited there. This design limits
credential replay; it does not make a provider-controlled machine confidential.

```bash
uv sync --group dev
uv run pytest
scripts/targon-smoke.sh inventory
scripts/targon-smoke.sh list
scripts/targon-smoke.sh kaniko-probe --resource cpu-small --roundtrip
scripts/targon-smoke.sh runtime-probe
scripts/targon-smoke.sh source-review-probe --image registry.example/screener@sha256:DIGEST
```

Authenticated workload operations use Targon's organization-scoped v3 API.
Production is pinned to the non-secret `ditto` organization slug; operators may
override it for smoke tests with `TARGON_ORG_SLUG`.

Production runs `python -m screener_capacity.controller` as a separate systemd
unit from Platform so provider retries, spend controls, and capacity locks do
not share the API process lifecycle.

Trusted monorepo image builds run in the separate
`python -m screener_capacity.builder` process. Platform supplies an allowlisted
component, exact SHA, Dockerfile, and destination. The builder mints a 30-minute
Artifact Registry writer token, starts one Kaniko rental, stores only the digest
and redacted status, and deletes the rental. Provider failure becomes
`fallback_required` for the existing build runner and never changes the hostile
screener-runtime capability gate.

Miner submission builds use the same separately locked process but a different
contract. Platform mints a short-lived capability bound to one build, screening
attempt, source object, and temporary output object. The rental receives only
the public Platform URL, build ID, and that capability; it receives no Targon,
GCP, registry, screener, controller, GitHub, validator, wallet, or inference
credential. The helper verifies the source SHA-256, runs the immutable Kaniko
executor without push/cache, uploads one bounded tar archive, and asks Platform
to hash every output byte. The owning GCE screener independently hashes and
imports the archive, applies the existing gates, then deletes the temporary
object. Rental deletion failure is recorded after suspension as an operator
cleanup event; zero replicas is the cost-safety boundary.

After Platform verifies every archive byte, the trusted controller impersonates
`ditto-screening-candidate-push` and promotes the exact archive into the private
one-day candidate registry with a short-lived writer token held only in a
mode-0600 auth file. Targon receives a distinct 30-minute
`ditto-screening-candidate-pull` reader token, launches that digest as the
Rental itself, and probes its proxied `/health`; no nested Docker is used. This
result is advisory until provider egress containment is qualified. Failure or
operator disablement releases the same verified archive to the GCE worker, which
performs the authoritative isolated runtime gate. Trusted Kaniko rentals still
use `ditto-image-builder`, which can write only `ditto-public-runtime`.

A source-review Rental uses the pinned reviewed screener image, one
attempt-bound Platform capability, and a short-lived bootstrap token for the
single model-key Secret Manager resource. Only a certified low-risk L1 result
may be reused. Suspicious, elevated, inconclusive, invalid, or unavailable
results always run through the existing local L2/L3 reviewer.

The repeatable `source-review-probe` launches a deterministic HTTPS-proxied
Platform/model mock and the exact reviewed screener digest as two disposable
Rentals. It verifies archive download and hashing, bounded read-only tools, the
typed low-risk completion contract, and DELETE-first cleanup without using a
real model-provider key. It may not make an elevated source verdict or replace
the GCE-owned isolated fake-gateway health/oracle gate, signing, and upload.
