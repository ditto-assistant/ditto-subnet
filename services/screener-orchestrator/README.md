# Screener orchestrator

This process is the sole normal writer of screener capacity. It reads runnable
demand from Platform, acquires a fenced controller lease, tries safe Targon
capacity first, and resizes the GCE managed instance group only for the residual
deficit. Both providers may return to zero when the queue and active leases are
empty.

Current Targon Rentals are approved only for the dedicated Kaniko image-builder
path. Hostile screener execution remains fail-closed behind an expiring
capability attestation because the nested RootlessKit probe failed. With a
`nogo` or missing attestation, all runtime demand falls back to GCE.

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
