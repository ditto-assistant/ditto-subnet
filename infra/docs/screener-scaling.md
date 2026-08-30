# Federated screener capacity

The normal production path is one fixed-cost 64 GB Hetzner node named
`subnet-screener-1`, with the existing GCE MIG retained at zero as outage and
backlog-overflow capacity. Platform owns admission and leases; Backroom owns
the revisioned provider and per-node concurrency settings. See
[`docs/hetzner-screener-fleet.md`](../../docs/hetzner-screener-fleet.md) for the
default-Debian install and Ansible runbook.

## Provider order and safety gate

Nested-Docker Targon screener slots remain retired. For each reconciliation the
controller:

1. reads the current Platform demand and renews its fenced lease;
2. reads the audited Backroom provider revision;
3. treats the configured Hetzner node heartbeat as the primary availability
   signal;
4. keeps GCE at zero while the primary is ready and unclaimed backlog is at or
   below `max(min_backlog, screening_concurrency * backlog_multiplier)`;
5. adds only residual GCE capacity above that threshold, or full bounded GCE
   capacity when the primary is not ready;
6. scales GCE down only after GCE-owned leases finish.

Production uses `['hetzner', 'gcp']` for build, runtime smoke, and source review.
The second entry means that separate GCE workers may claim still-unclaimed
submissions when the capacity policy activates them. It does not mean a failed
Hetzner lane is retried on GCE.

- **Hetzner primary** (`['hetzner', 'gcp']`): one full worker process starts the
  canary under one enrolled node identity. After the canary, Platform initially
  admits two build/smoke KVM slots and two review slots on the 64 GB host, with
  a matching two local worker processes.
- **GCE-only** (`['gcp']`): an audited emergency posture in which GCE workers
  run the whole build, smoke, and review pipeline locally.
- **Targon-only** (`['targon']`): retained for rollback compatibility, not the
  normal fleet posture.

Within one submission, static execution-safety preflight runs first, followed
by build, runtime smoke, general source review, and verdict. General review is
never leased before the exact attempt has both a successful build and smoke.
Different submissions move through those stages concurrently.

A revisioned write requires compare-and-swap, an audit reason, and an exact
confirmation string covering all three lists and the overflow policy. Node
screening, shared sandbox, build, runtime, and review ceilings have a separate
append-only control. New nodes default to zero capacity.

## Retired Targon one-shot notes

The following describes the retained Targon implementation and its rollback
contracts; it is not the normal Hetzner-primary posture.

Screening on Targon is Kaniko compile, direct-image `/health` smoke of that
exact archive, and L1 then L2/L3 review of the extracted source tarball in one
screener rental (Cloud Run Job is the capacity fallback). The rental is already
isolated, so L2 analyzers run in-process instead of nested Docker. Platform
admits the screening attempt and attests the verdict. GCE screener VMs are not
required for L2/L3. A screener-to-smoke-rental prompt tool is a later issue;
isolated fake-gateway oracle is skipped until then.

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
- The autoscaled GCE screener workers and capacity-controller VM are retained
  for bounded outage/backlog overflow and return to zero after GCE-owned leases
  drain. The superseded static `ditto-screener-prod` pet is retired.
- `subnet-screener-1` keeps its rotating enrolled-node credential only on the
  trusted host. Disposable build guests receive one build capability; smoke
  guests receive no Platform secret. Neither guest receives the review key.
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

No repository merge deploys or mutates production. Keep the existing GCE MIG
available at zero, merge and deploy Platform/Backroom/controller support, then
follow the dedicated-host runbook. Enrollment alone grants zero capacity.

First rehearse the public host role on Terraform's optional
`subnet-screener-dev-1` (`n2-standard-16`, nested KVM, no runtime secrets), then
return `enable_screener_fleet_dev_host` to false and prove the disposable VM is
absent. The dedicated-host runbook contains the exact protected workflow and
Ansible commands.

After `subnet-screener-1` is converged, use Backroom to:

1. verify its node ID, Hetzner resource ID, exact release SHA, image digest,
   heartbeat, and zero effective limits;
2. keep existing provider routes and every node limit at zero while host-local
   cold build, smoke, failed-build/no-review, and failed-smoke/no-review probes
   pass (shadow mode);
3. append the one-lane canary setting
   `SCREENING=1 SANDBOX=1 BUILD=1 RUNTIME=1 SOURCE_REVIEW=1`;
4. set all three provider lists to `['hetzner', 'gcp']` and enable overflow for
   `subnet-screener-1` at multiplier 3, minimum backlog 12, maximum 6;
5. prove one production build -> smoke -> source-review sequence and one
   build failure that never obtains a review lease;
6. raise the 64 GB node to
   `SCREENING=2 SANDBOX=2 BUILD=2 RUNTIME=2 SOURCE_REVIEW=2`, set the private
   inventory to two worker processes, and prove two simultaneous cold
   build/smoke lanes without memory or disk pressure; raise to three only after
   measured sandbox-plus-review memory leaves safe host margin;
7. exercise one controlled stale-heartbeat event and one above-threshold queue,
   proving GCE claims new work, preserves active leases, and returns to zero;
8. drain retired nested-Docker Targon worker nodes. Do not re-enable them.

The exact Debian, inventory, vault, Ansible, activation, verification, and drain
commands live in [`docs/hetzner-screener-fleet.md`](../../docs/hetzner-screener-fleet.md).

## Rollback

Apply all three lanes as GCE-only to restore the GCE screening path after an
audited Backroom revision. Stop the controller unit only after explicitly
setting the GCE MIG to a safe nonzero target or confirming the queue is empty. The
`ONLY_SCALE_OUT` watchdog is intentionally incapable of deleting workers during
a controller outage.

Drain `subnet-screener-1` before host maintenance. Removing the node, GCE
resources, or any deletion protection remains a separate reviewed operator
action.
