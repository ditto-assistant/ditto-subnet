# Screener orchestrator

This process is the sole normal writer of GCE screener capacity. It reads
runnable demand from Platform, acquires a fenced controller lease, and applies
the audited Backroom revision: Targon-first decomposed lanes keep the GCE MIG
at zero, and a GCE-only cutover scales residual demand onto the fleet. A stored
`['gcp', 'targon']` list is not a working hybrid; first-provider wins, so that
list disables Targon for the lane. Nested-Docker Targon screener slots are
retired: leftover `ditto-screener-*-slot-*` rentals are drained and deleted,
never created.

Targon Rentals have three independently controlled one-shot jobs owned by
Platform: credential-minimal Kaniko builds, direct-image runtime health checks,
and L1/L2/L3 source review in the same screener rental (in-process analyzer,
no nested Docker). Cloud Run is the capacity fallback. GCE remains only the
GCE-only revision cutover, not the L2/L3 path.

Provider credentials are accepted only through mode-0600 files. The operator
smoke wrapper streams `TARGON_API_KEY` directly from GCP Secret Manager to the
process over stdin; it never exports or prints the value.

## Provider trust boundary

Targon controls the rental runtime and can inspect every workload environment
variable, command, mounted file, process, and memory page. Workload env is not a
secret boundary. The controller therefore never sends the Targon API key or a
long-lived GCP credential to a rental. Trusted-builder and source-review
one-shots mint only a 30-minute GCP access token scoped to one Secret Manager
resource; the helper consumes it into a mode-0600 file and removes it from the
environment immediately.

```bash
uv sync --group dev
uv run pytest
scripts/targon-smoke.sh inventory
scripts/targon-smoke.sh list
scripts/targon-smoke.sh state wrk-xxxxxxxxxxxxxxxx
scripts/targon-smoke.sh logs wrk-xxxxxxxxxxxxxxxx --tail 400 --include-state
scripts/targon-smoke.sh sweep-oneshots
scripts/targon-smoke.sh sweep-oneshots --apply --workers 8
scripts/targon-smoke.sh kaniko-probe --resource cpu-small --roundtrip
scripts/targon-smoke.sh runtime-probe
scripts/targon-smoke.sh source-review-probe --image registry.example/screener@sha256:DIGEST
scripts/targon-smoke.sh source-review-probe --image DIGEST --starter-kit --live-model --review-timeout-seconds 1800
scripts/targon-screen-starter-kit.sh --tiny
scripts/targon-screen-starter-kit.sh
```

Miner screening on Targon is Kaniko `--destination=ditto-screen/{agent}-{attempt}:latest --no-push --tar-path=...`. Pin `screened_image_id` to the docker-save **config** digest from that tar, never Kaniko `--digest-file` or an Artifact Registry manifest digest. `targon-screen-starter-kit.sh` docker-builds the starter-kit harness (or `--tiny`) with that destination and fails closed if DittoBench would reject the archive.

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
`fallback_required` for the existing build runner and never recreates
nested-Docker Targon screener workers.

Miner submission builds use the same separately locked process but a different
contract. Platform mints a short-lived capability bound to one build, screening
attempt, source object, and temporary output object. The rental receives only
the public Platform URL, build ID, and that capability; it receives no Targon,
GCP, registry, screener, controller, GitHub, validator, wallet, or inference
credential. The helper verifies the source SHA-256, runs the immutable Kaniko
executor without push/cache, uploads one bounded tar archive, and asks Platform
to hash every output byte. The owning GCE screener independently hashes and
imports the archive, applies the existing gates, then deletes the temporary
object. Rental charges run from create until DELETE, including `suspended` time. The
builder holds a finished job until DELETE, requests a non-persistent rental,
and sweeps leftovers by bringing them to `running` only long enough to DELETE.
A remaining record is an operator cleanup event.

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
single model-key Secret Manager resource. L1 then L2/L3 run in that rental
with an in-process analyzer. The completed observation is authoritative;
GCE does not re-review.

The repeatable `source-review-probe` launches a deterministic HTTPS-proxied
Platform/model mock and the exact reviewed screener digest as two disposable
Rentals. It verifies archive download and hashing, bounded read-only tools, the
typed low-risk completion contract, and DELETE-first cleanup without using a
real model-provider key. It may not make an elevated source verdict or replace
the GCE-owned isolated fake-gateway health/oracle gate, signing, and upload.
