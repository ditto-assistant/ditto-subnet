# On-demand AWS screener node (`subnet-screener-3`)

The AWS primary replaces the always-on Hetzner host `subnet-screener-1`. It is
one EC2 instance (`c7a.2xlarge`, 200 GB gp3, Debian 12) that is **stopped most
of the time**:

```text
backlog → controller starts EC2 (wake role, keyless) → worker enrolls/refreshes
→ builds + smoke → idle 15 min → worker exits 85 → host powers itself off (stop)
```

- **Wake.** The GCE capacity controller sees runnable work, an open and active
  `aws` primary that is not ready, and a stopped instance. It starts the
  instance under its fenced lease and reports `AWS_PRIMARY_WAKING`. Its Google
  ID token assumes `ditto-screener-capacity-wake`, which may only describe
  instances and start the `stack=aws-screener` one. No AWS key exists.
- **Grace.** If the node is not ready within 600 s, or the start fails, the
  backlog overflows to GCE exactly as for a Hetzner outage
  (`AWS_PRIMARY_UNAVAILABLE`). An idle, stopped node is `AWS_PRIMARY_ASLEEP`.
- **Sleep.** Each worker exits with status 85 after
  `SCREENER_IDLE_EXIT_SECONDS` (900) without work. `ditto-screener-idle-shutdown.timer`
  powers the host off once every worker idle-exited, no lease file exists, and
  the release updater is idle. EC2's instance-initiated shutdown behavior is
  `stop`, so the EBS root keeps the node credential, checkout, and build cache.
- **Auth after sleep.** Platform lets an `aws` node rotate the bearer it held
  when it stopped, with a fresh hotkey signature, for up to
  `SCREENER_NODE_REAUTH_MAX_DORMANCY_SECONDS` (30 days). A node asleep longer
  must re-enroll (step 4 below).
- **Releases.** The signed pull updater runs 1 minute after every boot, so a
  woken node adopts the current release after startup. Workers can claim on
  the persisted release before that update finishes; keep the first-boot pin
  valid and verify the reported release before opening admission after repair.
  Debian 12 installs Buildx separately from the signed Docker apt repository;
  the worker invokes `docker buildx build` explicitly and refuses to start
  without the plugin, while retaining the distribution rootless daemon.

Cost guards, so nobody has to watch it:

- A host whose workers never idle-exit powers off after 6 h up with no active
  lease (`SCREENER_IDLE_MAX_UPTIME_SECONDS`).
- A host that has not enrolled 2 h after boot powers off
  (`ditto-screener-unenrolled-poweroff.timer`, installed first by user data),
  deferring only while the exact first-boot process is alive. The whole first
  boot runs under a 4 h deadline, so this ends within about 4.5 h.
- The controller never stops or terminates the instance, and Terraform never
  starts or stops it.

## One-time stand-up

Prerequisites: #3147 (AWS groundwork), the DITTO-2594 stack, and the change
that wires the AWS roots into `infra-plan-apply.yml` (it needs that
workflow's independent admin review) are merged and released, so Platform, Backroom, and the controller accept `aws`. Nothing below
happens automatically; each step is a reviewed operator action.

1. **Identities (SSO admin, local).** Apply `infra/terraform/stacks/aws-bootstrap`
   with `enable_aws_screener = true` and
   `screener_capacity_controller_unique_id` = the `gcp-platform` output
   `screener_capacity_controller_unique_id`. This creates the node role and
   profile, the wake role, and the CI grants for the `aws-screener` root. See
   `infra/docs/ci-bootstrap.md`.
2. **GCP federation (protected workflow).** Plan/apply root `gcp-platform` with
   `enable_screener_aws_identity = true`. Record the `screener_aws_wif_provider`
   output. This creates the `screener-aws` pool, the secret grants for the
   `ditto-screener-node` role, and the empty
   `screener-aws-registration-token` secret.
3. **Instance (protected workflow).** Plan/apply root `aws-screener` with
   `enable_aws_screener = true`, an exact `screener_fleet_release_sha`, the
   digest-pinned `screener_fleet_builder_image`, and
   `screener_fleet_aws_wif_provider` (the step 2 output), and
   `screener_fleet_idle_exit_seconds = 900`. The plan refuses to enable the
   root without it, and the first boot refuses a release whose worker cannot
   idle out. The instance boots and converges
   itself. Its first converge stops at enrollment because the grant does not
   exist yet; the host stays up (up to 2 h) for the next step.
4. **Enroll (Backroom + gcloud, within the grant TTL).** Read the controller
   epoch with `get_screener_capacity`, then `create_screener_bootstrap_grant`
   for node `subnet-screener-3`, provider `aws`, resource ID = the
   `aws-screener` output `screener_instance_id`. Store the returned token without printing
   it:

   ```bash
   pbpaste | gcloud secrets versions add screener-aws-registration-token \
     --project ditto-app-dev --data-file=-
   ```

   Then re-run the first-boot script over SSM Session Manager:
   `sudo bash /var/lib/cloud/instance/scripts/part-001`. It consumes the grant
   and deletes the local copy. Disable the secret version afterwards.
5. **Shadow, then admit.** As for any new node, verify identity, release, and
   heartbeat in Backroom at zero capacity, then append
   `SCREENING=1 SANDBOX=1 BUILD=1 RUNTIME=1 SOURCE_REVIEW=1 CANARY=1`.
6. **Controller.** Converge the controller host with
   `screener_capacity_aws_primary_instance_id` (the instance ID) and
   `screener_capacity_aws_primary_role_arn` (the `aws-bootstrap` output
   `screener_capacity_wake_role_arn`).
7. **Route.** Apply a provider revision with all three lanes `aws > gcp`,
   overflow enabled, primary `subnet-screener-3`.
8. **Drill.** With the queue empty, wait for the host to stop itself
   (`AWS_PRIMARY_ASLEEP`). Upload one starter-kit submission and confirm
   `primary_wake_requested`, `AWS_PRIMARY_WAKING`, a ready heartbeat, a
   signed verdict, and the host stopping again about 15 min later. Then stop
   the controller briefly to confirm the GCE watchdog still serves backlog.
9. **Retire Hetzner.** Robot server #3062657 (`subnet-screener-1`) was
   cancelled on 2026-11-14 after explicit permanent-deletion approval. All
   admission and active-use counters were zero; node-control revision 16
   records permanent closure. Do not restore its historical admission limits
   or use the cancelled hardware as rollback capacity. GCE is the remaining
   fallback; its separate controller-outage drill is still pending.

   The checked-in retirement intent disables the old X.509 service account,
   pool, provider and secret grants. Apply only through the protected
   `gcp-platform` plan/apply workflow, with
   `screener_fleet_x509_identity_enabled=false`. Before approving, require
   a plan containing only the retired identity removals and intended outputs;
   preserve the AWS pool, its review-key access, the controller, shared
   pool-administration grant and every VM. Source intent is not evidence that
   the identity removals have been applied.

## Operate

- **State.** Backroom `get_screener_capacity` (reason codes and events). The
  EC2 console or `aws ec2 describe-instances` show stopped and running.
- **Shell.** SSM Session Manager only; the security group admits no ingress.
- **Logs.** `journalctl -u 'ditto-screener-worker@*' -u ditto-screener-idle-shutdown`.
  The first-boot log is `/var/log/ditto-screener-bootstrap.log`.
- **Keep it awake** for maintenance: set the node `draining` in Backroom (the
  controller then never wakes it), start it from the console, and stop
  `ditto-screener-idle-shutdown.timer`.
- **Replace the instance.** Taint and re-apply `aws-screener`. The new instance
  has a new resource ID and an empty disk, so repeat step 4 for a new node ID
  or revoke the old node first.

## Not yet proven on a real host

These are checked only by unit tests, renders, and `terraform validate`/`test`
with mock providers:

- `gcloud ... create-cred-config --aws --enable-imdsv2` from the instance role;
- rootless Docker DNS through the VPC's link-local resolver (169.254.169.253);
- the Google-to-AWS web-identity condition keys (`aud`/`azp`, `oaud`, `sub`);
- `c7a.2xlarge` availability in `us-east-1a`.

The drill in step 8 is the acceptance test.
