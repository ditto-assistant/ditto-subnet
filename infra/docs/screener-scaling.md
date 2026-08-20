# Federated screener capacity

The subnet control plane can run with zero idle GCE screeners. Platform owns
demand, admits Targon screening attempts, creates the Kaniko / runtime / L1
rentals, and attests verdicts. The separate `ditto-screener-capacity-prod` VM
and screener fleet MIG are retired on this path.

## Provider order and safety gate

Nested-Docker Targon screener slots are retired. There is no GO/NOGO
hostile-runtime attestation and no persistent `ditto-screener-prod-*` worker
lane. For each reconciliation the controller:

1. reads the current Platform demand and renews its fenced lease;
2. reads the audited Backroom provider revision;
3. keeps the GCE MIG at zero when all three decomposed lanes are Targon-first,
   or resizes it to residual demand for a GCE-only / mixed revision;
4. drains leftover nested-Docker Targon slots before deletion and scales GCE
   to zero only when Platform reports no active screening leases.

Backroom controls build, direct-image runtime smoke, and source review as three
independent provider lists. Each list always retains GCP as the safety fallback.
Production has two postures, not a working hybrid:

- **Targon-first** (`['targon', 'gcp']`): Platform claims new one-shot Targon
  work for that lane, then falls back to Cloud Run when Targon has no
  capacity, already has `max_inflight` (default 10) live rentals, or a
  rental never leaves provisioning within `provision_timeout_seconds`.
  Kaniko and L1 are Cloud Run Jobs. Runtime smoke is a short-lived internal
  Cloud Run Service so Platform can GET `/health`. The untrusted runtime SA
  receives no cloud, GitHub, Platform, or provider credentials — only the
  attempt-bound job token.
- **GCE-only** (`['gcp']`): Targon is disabled for that lane. Queued Targon
  work is immediately terminalized as `fallback_required` / runtime `skipped`.
  This is the old GCE screening path and does not require a deploy.

The first provider wins. A stored `['gcp', 'targon']` list is accepted because
GCP is present, but it is not a working "GCP first then Targon fallback"; it
behaves exactly like `['gcp']`. Backroom exposes per-lane Targon-first and
Targon-off controls.

A revisioned write requires compare-and-swap, an audit reason, and an exact
confirmation string covering all three lists. In-progress one-shot jobs finish;
new and queued jobs follow the new revision.

Screening on Targon is Kaniko compile, direct-image `/health` smoke of that
exact archive, and read-only L1 review of the source tarball in a separate
screener rental. Platform admits the screening attempt and attests the verdict.
Elevated L1 findings quarantine rather than running GCE L2/L3. A
screener-to-smoke-rental prompt tool is a later issue; isolated fake-gateway
oracle is skipped until then.

The GCE screener fleet and the capacity-controller VM are leftover from the
nested-Docker path except for trusted-image builds and GCE-only cutover. Platform
itself creates Targon one-shot rentals from a background loop in the API
process. With Targon-first lanes, scale the screener MIG to zero. Do not recreate
persistent Targon screener slots.

Ordinary agent workloads do work in Rentals. The secret-free `agent-probe`
installs pinned OpenCode and Pi releases, executes both binaries, and then
deletes the disposable Rental. This proves the provider can host agentic source
analysis; it does not authorize either generic agent to make screening
decisions. The production screener already has a bounded L1/L2/L3 reviewer and
typed, digest-bound findings. Reusing that reviewer in a credential-minimal
source-analysis sub-job is safer than giving a general coding agent unrestricted
tools. The production source-review sub-job accepts only certified low-risk L1
output; all other observations fall back to the GCE-owned L2/L3 review under the
same screening attempt.

The direct-image `runtime-probe` is independently reproducible. It launches a
plain image with a proxied port, checks `/health`, and deletes the Rental. A live
probe on 2026-08-13 returned `200 ok` while provider state still said
`provisioning`, then DELETE succeeded. Runtime admission therefore follows the
actual HTTP contract rather than the provider readiness counter. Candidate
images are private, digest-pinned, and expire after one day. Until Targon egress
policy is independently qualified, this remote result is telemetry/advisory and
the GCE isolated smoke remains authoritative.

Submission builds give Targon Kaniko a dedicated 25-minute window. That timeout
is independent of the local Docker cap: the 70-minute screening lease can spend
up to 25 minutes remotely and still preserve up to 45 minutes for GCE fallback.
Changing a local build override must never silently shrink the Targon window.
Transient Targon lifecycle and Platform status calls are retried and reconciled
before fallback is authorized. When fallback is necessary, its public error code
separates rental start, builder runtime, source, Kaniko, archive, upload,
verification, timeout, and Platform-control failures without copying provider
responses or untrusted build logs into Platform.

One-shot builder rentals use this Targon contract (vendor-confirmed
2026-08-17, plus our live probes):

- `DELETE` of a **running** rental returns HTTP 204, then the record often
  flips to `error` / exit 137 within ~30s. That is SIGKILL of the running
  container. Treat it as teardown complete. Do not redeploy those
  tombstones. Targon says billing stopped at that `error` in their tests;
  we have not independently metered that claim against our invoices.
- `GET /workloads` and the dashboard keep those rows as `deleted` or
  `error` 137 for a while, then they age out. A second `DELETE` is a no-op
  204. Soft-delete is not a purge.
- `DELETE` of `suspended` / `error` / `registered` still returns HTTP 500.
  Real leftovers (job crash, not post-DELETE 137) are patched onto a public
  `busybox` sleep image and brought to `running` only long enough for
  `DELETE`. Never resume the original crashing image.
- Suspend is not a parking lot. Our Aug 2026 billing table showed
  multi-hundred-hour charges on old suspended one-shots. Targon has not
  yet confirmed or denied suspend billing. Keep treating suspend as billed
  until they do.
- PID-1 exit can restart a persistent rental. Targon has not reproduced a
  sustained crash-loop. We still hold the successful builder until
  SIGTERM/DELETE so a consumed job does not restart.
- Do not send `experiments.persistent-workload`. That key is config-gated
  and Targon rejects it with HTTP 400.

Leftover nested-Docker screener slot rentals are drained by the capacity
controller and are never swept by `sweep-oneshots`.

```bash
scripts/targon-smoke.sh sweep-oneshots
scripts/targon-smoke.sh sweep-oneshots --apply
```

The GCE autoscaler remains as an independent `ONLY_SCALE_OUT` watchdog on the
group-level backlog metric. It cannot scale in or fight the controller. Its
floor is zero; normal scale-in is controller-owned and lease-aware.

## Identity boundaries

- Platform API uses `ditto-platform-api`. It admits Targon screens, creates
  rentals, and attests verdicts. Terraform grants that identity
  `secretAccessor` on `TARGON_API_KEY`. Ansible reads the version at converge
  under `no_log` and renders `DITTO_TARGON_API_KEY` into the platform `.env`
  like every other platform secret. The value is never logged or placed in
  rental env except the attempt-bound job tokens already required for Kaniko/L1.
- GCE screener workers, `ditto-image-builder`, and the capacity-controller VM
  are leftover from the nested-Docker path and are not required.
- Federated nested-Docker Targon workers are retired. Source-review one-shots
  still receive a 30-minute token for `ditto-screener-bootstrap`, which can
  read only the source-review secret. No service-account key crosses the
  provider boundary.
- Submitted builds receive none of these credentials. GCE hostile builds run
  behind the dedicated rootless executor and metadata/egress guards. A Targon
  submission-builder rental receives only one expiring Platform capability
  bound to an attempt, source object, and temporary output object. It cannot
  push registry images or read Secret Manager.
- The trusted controller promotes a Platform-verified archive to the private
  `ditto-screening-candidates` repository by impersonating
  `ditto-screening-candidate-push`. That writer token stays on the host and
  never crosses the provider boundary. Targon receives a separate 30-minute
  `ditto-screening-candidate-pull` reader token as pull auth. `ditto-image-builder`
  can write only `ditto-public-runtime` and is never granted candidates write.
- Source-review jobs receive one attempt token and one 30-minute bootstrap token
  for the source-review secret. They read but never execute submitted source.
- Trusted release builds run in the separate `ditto-image-builder` service.
  It gives a Targon Kaniko rental one 30-minute OAuth token for
  `ditto-image-builder`, which can write only `ditto-public-runtime`. The
  rental cannot read Secret Manager or impersonate the controller. A distinct
  prod-environment `github-actions-subnet-build` identity owns the existing
  runner fallback and publishing the reviewed Kaniko executor.

`TARGON_API_KEY` is never a Terraform value. Terraform resolves only the Secret
Manager resource metadata and grants `ditto-platform-api` (and the leftover
capacity VM, while it exists) `secretAccessor`. Ansible reads the version with
the Platform VM's attached identity under `no_log` and renders it into `.env`.
Local operator probes use the monorepo's
`services/screener-orchestrator/scripts/targon-smoke.sh`, which streams the
secret directly from Secret Manager to the client process. The provider client
uses Targon's organization-scoped v3 workload API and pins the non-secret
production organization slug to `ditto`.

The three one-shot lanes have separate disposable smoke commands: a Kaniko
`--roundtrip` build, a direct-image `runtime-probe`, and a
`source-review-probe` against an exact reviewed screener digest. The source
probe supplies a deterministic Platform/model mock and a fake probe-only model
key; production Secret Manager bootstrap remains covered by the controller and
worker contract tests.

## Stand-up order

No repository merge deploys or mutates production. After the destination and
infra stacks merge, use this order:

1. Apply with `enable_screener_fleet_secrets=true`, the existing pet/fleet flags
   preserved, and `enable_screener_capacity_controller=false`. Populate the
   `screener-repo-deploy-key-prod` and `screener-controller-api-token-prod`
   secret versions out of band. Register only the public deploy-key half on
   `ditto-assistant/ditto-subnet`.
   Record `subnet_build_sa_email`, `dittobench_deploy_sa_email`,
   `platform_deploy_sa_email`, and `wif_provider` as the matching protected
   `prod` GitHub environment secrets. Copy the existing Cloudflare deploy
   credentials into that environment for the subnet-only Backroom release.
2. Converge the prod Platform host so it reads the separate controller bearer.
   Source every documented `PLATFORM_*` value before this play; its fail-closed
   preflight prevents placeholder configuration.
3. Apply `enable_screener_fleet=true` with
   `screener_fleet_min_replicas=0`. Verify the MIG can resize 0 -> 1 -> 0 using
   a disposable queue/test environment before cutting over real demand.
4. Publish the pinned maintained Kaniko executor with the monorepo's
   `Publish Maintained Kaniko Executor` workflow. Then apply
   `enable_screener_capacity_controller=true` and converge:

   ```bash
   GCP_OSLOGIN_USER=... ansible-playbook -i infra/ansible/inventory/gcp.yml \
     infra/ansible/playbooks/gcp-screener-capacity.yml
   ```

5. Verify Platform Backroom reports the applied provider revision, one-shot
   provider jobs, capacity events, and trusted build state. Exercise each
   Backroom lane control in a disposable environment before using it in
   production. Queue one release image and prove Targon output by immutable
   digest; if Rental inventory is empty, prove the audited GCP fallback instead.
   With Targon-first decomposed lanes, an empty queue must return the MIG to
   zero. Then enqueue one audited miner rebuild. Prove the submission-builder
   image is digest pinned, Platform verifies the complete tar SHA-256, the
   Targon rental is deleted (or leaves a cleanup-required event).
   The artifact bucket's `remote-builds/` lifecycle is the final one-day bound
   for a canceled presigned upload that races normal cleanup.
6. Do not re-enable nested-Docker Targon screener workers. Drain leftover
   `ditto-screener-prod-*` slots through the capacity controller.

## Rollback

Draft all three lanes to GCE-only to restore the GCE screening path after the
audited apply. Stop the controller unit only after explicitly setting the GCE
MIG to a safe nonzero target or confirming the queue is empty. The
`ONLY_SCALE_OUT` watchdog is intentionally incapable of deleting workers during
a controller outage.

Do not retire the pet VM until the zero-idle GCE fallback has handled a real
burst and the exact-sha monorepo deploy has been verified. Retirement remains a
separate, reviewed Terraform and operator action because the pet resource has
deletion protection.
