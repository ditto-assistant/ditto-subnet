# Federated screener capacity

The subnet control plane can run with zero idle screeners. Platform owns demand,
controller fencing, one-time node enrollment, rotating node credentials, and the
audited capacity/event view. The private `ditto-screener-capacity-prod` process
is the only normal provider mutator.

## Provider order and safety gate

For each reconciliation, desired slots are bounded by the global cap and include
every active lease. The controller then:

1. reads the current Platform demand and renews its fenced lease;
2. reads the audited Backroom provider revision and uses Targon only when it is
   first in the relevant provider list; full workers additionally require a
   fresh `go` hostile-runtime capability attestation;
3. resizes the GCE regional MIG for the residual demand;
4. drains nodes before deletion and scales both providers to zero only when
   Platform reports no active screening leases.

Backroom controls build, direct-image runtime smoke, and source review as three
independent provider lists. Each list always retains GCP as the safety fallback.
Production has two postures, not a working hybrid:

- **Targon-first** (`['targon', 'gcp']`): Targon claims new work for that lane.
- **GCE-only cutover** (`['gcp']`): Targon is disabled for that lane. Queued
  Targon work is immediately terminalized as `fallback_required` / runtime
  `skipped`. This is the old GCE screening path and does not require a deploy.

The first provider wins. A stored `['gcp', 'targon']` list is accepted because
GCP is present, but it is not a working "GCP first then Targon fallback"; it
behaves exactly like `['gcp']`. Backroom therefore exposes only Targon-first and
Targon-off, plus an emergency "Cut over to GCE only" control that drafts all
three lanes to `['gcp']` for the existing audited apply.

A revisioned write requires compare-and-swap, an audit reason, and an exact
confirmation string covering all three lists. In-progress one-shot jobs finish;
new and queued jobs follow the new revision. After Targon is validated, remove
the cutover UI and delete the GCE VMs/MIG via Terraform. Do not delete them now.

The current Targon Rentals attestation is `nogo`. Live disposable probes found
that rootful Docker cannot mount its required kernel filesystems, rootless
Docker cannot create its user namespace, BuildKit cannot perform the first bind
mount, and plain Rentals expose neither `/dev/fuse` nor `/dev/kvm`. These are
provider sandbox limits, not missing worker glue. A missing, invalid, expired,
or `nogo` attestation routes full-worker demand to GCE. It does not disable the
three decomposed jobs. Targon may compile a miner submission, launch the exact
Platform-verified image directly as a Rental, and perform bounded read-only L1
review. It may not make an elevated source verdict or replace the GCE-owned
isolated fake-gateway health/oracle gate, signing, and upload.

Targon VMs expose a stronger kernel boundary, but they are not an acceptable
autoscaling substitute yet: the live inventory is GPU-only, bootstrap is SSH
and password based, and a disposable VM delete returned a provider error after
entering termination. Keep the VM probe operator-only until CPU inventory and a
reliable delete lifecycle exist. Never turn that exploratory path into managed
capacity merely because its nested-runtime probe passes.

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

One-shot builder, runtime, source-review, and probe rentals are deleted while
they are still running. Live Targon DELETE returns HTTP 500 for `suspended`,
`error`, and `registered` records, so the builder never suspends as a teardown
fallback. Leftover records in those states are redeployed and deleted
immediately. The same process sweeps leftover `ditto-miner-build-*`,
`ditto-build-*`, `ditto-runtime-*`, `ditto-source-*`, and `ditto-*-probe-*`
names. It never touches screener slot rentals or in-flight
`running`/`provisioning` work.
Operators can drain the current pile without waiting for a deploy:

```bash
scripts/targon-smoke.sh sweep-oneshots
scripts/targon-smoke.sh sweep-oneshots --apply
```

`--apply` redeploys leftover terminal records, waits until they are
`running`, then deletes. Recent `running`/`provisioning` jobs are skipped;
older ones are treated as leftover redeploys.

The GCE autoscaler remains as an independent `ONLY_SCALE_OUT` watchdog on the
group-level backlog metric. It cannot scale in or fight the controller. Its
floor is zero; normal scale-in is controller-owned and lease-aware.

## Identity boundaries

- Platform API uses `ditto-platform-api`; it reads the controller bearer only
  to authenticate controller calls.
- The capacity VM uses `ditto-screener-capacity`; it may read the Targon key and
  controller bearer, resize the screener MIG, and mint short-lived bootstrap
  tokens. It has no Platform storage or inference-provider access.
- GCE workers use `ditto-screener-worker`; they can read only the exact worker
  signing, bearer, source-review, and repository bootstrap secrets.
- Federated Targon workers get a one-time Platform registration grant and a
  30-minute token for `ditto-screener-bootstrap`, which can read only the
  source-review secret. No service-account key crosses the provider boundary.
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
Manager resource metadata. Ansible reads the version with the capacity VM's
attached identity under `no_log` and writes a mode-0600 file. Local operator
probes use the monorepo's `services/screener-orchestrator/scripts/targon-smoke.sh`,
which streams the secret directly from Secret Manager to the client process.
The provider client uses Targon's organization-scoped v3 workload API and pins
the non-secret production organization slug to `ditto`.

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

5. Verify Platform Backroom reports the controller heartbeat, desired slots,
   applied provider revision, Targon capability/reason, GCE target/health,
   capacity events, and trusted build state. Exercise each Backroom lane control
   in a disposable environment before using it in production. Queue one release
   image and prove Targon output by immutable
   digest; if Rental inventory is empty, prove the audited GCP fallback instead.
   With the
   current NOGO attestation, real demand must choose GCE and an empty queue must
   return the MIG to zero.
   Then enqueue one audited miner rebuild. Prove the submission-builder image is
   digest pinned, Platform verifies the complete tar SHA-256, GCE imports and
   finishes the normal screening gates, the temporary object is consumed, and
   the Targon rental is deleted (or leaves a cleanup-required event).
   The artifact bucket's `remote-builds/` lifecycle is the final one-day bound
   for a canceled presigned upload that races normal cleanup.
6. Only after a fresh hostile-runtime probe returns GO may an operator update
   the expiring Targon attestation and immutable worker image reference. Observe
   one shadow node through enrollment, heartbeat, a real lease, drain, and
   deletion before raising the cap.

## Rollback

Set the runtime attestation to `nogo` to drain Targon after active leases finish;
the controller immediately plans GCE for residual demand. Stop the controller
unit only after explicitly setting the GCE MIG to a safe nonzero target or
confirming the queue is empty. The `ONLY_SCALE_OUT` watchdog is intentionally
incapable of deleting workers during a controller outage.

Do not retire the pet VM until the zero-idle GCE fallback has handled a real
burst and the exact-sha monorepo deploy has been verified. Retirement remains a
separate, reviewed Terraform and operator action because the pet resource has
deletion protection.
