# Federated screener capacity

The subnet control plane can run with zero idle screeners. Platform owns demand,
controller fencing, one-time node enrollment, rotating node credentials, and the
audited capacity/event view. The private `ditto-screener-capacity-prod` process
is the only normal provider mutator.

## Provider order and safety gate

For each reconciliation, desired slots are bounded by the global cap and include
every active lease. The controller then:

1. reads the current Platform demand and renews its fenced lease;
2. uses healthy or pending Targon capacity first, but only with a fresh `go`
   hostile-runtime capability attestation;
3. resizes the GCE regional MIG for the residual demand;
4. drains nodes before deletion and scales both providers to zero only when
   Platform reports no active screening leases.

The current Targon Rentals attestation is `nogo`: nested RootlessKit did not
provide the isolation required for hostile miner Docker builds. A missing,
invalid, expired, or `nogo` attestation routes all demand to GCE. The dedicated
Kaniko build path is a separate capability: Targon may compile a miner
submission with an attempt-bound token, but it may not claim or decide screening
work. GCE still owns static preflight, archive import, health/source/policy
review, signing, upload, and local-build fallback.

Submission builds give Targon Kaniko a dedicated 25-minute window. That timeout
is independent of the local Docker cap: the 70-minute screening lease can spend
up to 25 minutes remotely and still preserve up to 45 minutes for GCE fallback.
Changing a local build override must never silently shrink the Targon window.

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
   Targon capability/reason, GCE target/health, capacity events, and trusted
   build state. Queue one release image and prove Targon output by immutable
   digest; if Rental inventory is empty, prove the audited GCP fallback instead.
   With the
   current NOGO attestation, real demand must choose GCE and an empty queue must
   return the MIG to zero.
   Then enqueue one audited miner rebuild. Prove the submission-builder image is
   digest pinned, Platform verifies the complete tar SHA-256, GCE imports and
   finishes the normal screening gates, the temporary object is consumed, and
   the Targon rental is suspended/deleted (or leaves a cleanup-required event).
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
