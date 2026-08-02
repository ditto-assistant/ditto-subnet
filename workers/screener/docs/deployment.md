# Production deployment

Production runs as the `ditto-screener` systemd unit on isolated GCE VMs: the
long-lived `ditto-screener-prod` host plus any instances of the autoscaled
screener fleet (a managed instance group sized by screening-queue depth; see
the infra repository's `docs/screener-scaling.md`). GitHub Actions
authenticates to GCP with Workload Identity Federation, discovers every
production screener by label (`env=prod`, `role=screener` or
`role=screener-fleet`), copies the updater over IAP, and deploys the exact
tested commit to each. The updater keeps the old process running through fetch
and dependency sync, installs the repository-owned systemd unit, restarts only
after the checkout is ready, verifies three consecutive systemd plus
authenticated read-only policy preflight checks, and rolls back both the code
and unit if the new process is not healthy. Instances younger than 15 minutes
are skipped: they are still executing first-boot bootstrap and converge to
`origin/main` on their own, and the five-minute scheduled run then brings them
to the exact deployed commit.

## Fleet instance bootstrap

Autoscaled instances provision themselves with zero manual steps. The GCE
instance template's startup script (owned by the infra repository) fetches the
read-only repository deploy key from Secret Manager, clones this repository,
and executes `scripts/bootstrap-screener.sh`, which:

1. installs Docker, uv, and the `deploy` service user, mirroring the pet VM's
   layout exactly (`/opt/ditto/screener`, `deploy:ditto`, protected
   `screener.env`);
2. materializes the worker secrets from Secret Manager — the shared screener
   hotkey mnemonic and platform bearer token (values never appear in logs or
   instance metadata);
3. hands off to `scripts/update-screener.sh` pinned to the checkout's HEAD, so
   first boot passes the same health verification as every subsequent deploy.

Every fleet instance shares the single allowlisted screener identity (hotkey,
sr25519 signing key, bearer token). The platform's lease claims are safe under
concurrency (`SKIP LOCKED` row claims, one running attempt per submission,
45-minute lease expiry), so a fleet drains the queue without coordination. The
known limitation is the fleet heartbeat: the platform keys heartbeats by
hotkey, so N workers collapse into one `/screeners` row until the platform
grows a per-worker identity dimension.

Scale-in note: instance deletion grants only ~90 seconds of shutdown, so an
in-flight build on a scaled-in instance is killed rather than drained
(`TimeoutStopSec` applies to operator stops, not GCE deletions). The platform
re-queues the interrupted submission when its lease expires; the autoscaler is
configured to scale in at most one instance per 20 minutes to bound this.

## Required GitHub secrets

Repository or `prod` environment secrets:

- `GCP_WIF_PROVIDER`: Workload Identity Provider resource name. Trust only this
  private repository and its `prod` environment.
- `GCP_SCREENER_DEPLOY_SA`: dedicated deploy service-account email. Grant only
  IAP tunnel, instance listing/lookup, and SSH access to the production
  screener instances (the fleet's instances are ephemeral, so these are
  project-level `compute.viewer`, `iap.tunnelResourceAccessor`, and
  `compute.osAdminLogin` rather than per-instance grants).
- `RELEASE_TOKEN`: fine-grained token or GitHub App token scoped only to this
  repository's contents, used for semantic-release commits, tags, and releases.

The production host additionally needs a private half of a read-only deploy key
in the deploy user's SSH configuration. Register only its public half as the
read-only `DITTO_SCREENER_REPO_DEPLOY_KEY` deploy key on this repository.

Runtime secrets stay in `/opt/ditto/screener/screener.env` or protected files on
the VM:

- `SCREENER_API_TOKEN`: bearer token shared with the platform API.
- `SCREENER_MNEMONIC`, or protected wallet files selected by
  `SCREENER_WALLET_NAME` and `SCREENER_WALLET_HOTKEY`.
- The file referenced by `SCREENER_GH_TOKEN_FILE`, when needed. Its token gets
  read-only contents access to only the private dependency repository.
- `SCREENER_AUDIT_SEED`, when the private random-control module is enabled.
- The protected files referenced by `SCREENER_POLICY_MANIFEST_FILE`, private
  module `feed_path`/`pack_path` values, and `SCREENER_REVIEW_JOURNAL_FILE`.
- `SCREENER_SOURCE_REVIEW_API_KEY_FILE`: mode-0400 OpenRouter key file readable
  only by the screener service user. The default reviewer model is
  `openai/gpt-5.6-luna`; every request enforces ZDR and denies data collection.
  Optional escalations use `moonshotai/kimi-k3` with ordered
  `z-ai/glm-5.2`, then `openai/gpt-5.6-sol` fallback for L2, followed by exact
  `openai/gpt-5.6-sol` for the independent L3 clearance critic. They reuse the
  same protected key file.

Never place any secret value, private challenge, private risk rule, or raw
artifact evidence in source, workflow arguments, logs, or PR text.

## Policy v7 rollout

1. Merge and deploy the platform protocol pin first. Existing v6 workers stop
   claiming because the queue advertises required policy version 7. Existing
   submissions and accepted validator scores are preserved.
2. Deploy the v7 worker. The updater materializes `validator-openrouter-key`
   from Secret Manager into a mode-0400 file and verifies that the platform
   requires the installed worker policy before declaring deployment healthy.
3. Verify a baseline pass, a quarantine path, signed results, heartbeats, and
   cache maintenance. Let any old-worker lease finish or expire; late or
   conflicting results remain rejected.
4. A protected private manifest remains an optional, reversible operator
   override. Timing and random-control selectors never terminally reject.

At every step, late, expired, conflicting, wrong-policy, wrong-agent, and
wrong-signer verdicts remain rejected. Existing waiting-validator/evaluating
rows, prior score receipts, screening history, and active leases are untouched.

## Cache and disk maintenance

Disk is bounded by two cooperating layers:

1. **Docker daemon builder GC** (`deploy/daemon.json`, installed by the
   updater; Docker restarts only when the file changes): BuildKit enforces the
   40 GB cache budget continuously, per build, so a heavy screening burst (for
   example a policy-rescreen wave that rebuilds every submission in a day)
   cannot outrun the scheduled pass. The budget is sized so that identical
   resubmissions and requeued re-screens keep hitting the layer cache
   (sub-second rebuilds) across at least a day of throughput: at 12 GB the
   cache sat pinned at its cap and evicted layers faster than the platform's
   ~45-minute re-screen cycle brought the same source back.
2. **Updater backstop**: every scheduled run (five minutes) performs bounded
   garbage collection at most once per hour — `docker builder prune
   --keep-storage` with NO age filter (an age filter exempts exactly the
   burst-created cache that overruns the budget), dangling-image pruning after
   a week, and in-place truncation of the service log above 64 MB. Tunables
   are `SCREENER_CACHE_GC_INTERVAL_SECONDS` and `SCREENER_CACHE_KEEP_STORAGE`.
Running containers and referenced images are never pruned.
