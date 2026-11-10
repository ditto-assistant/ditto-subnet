# Generator competition scorer

## Automatic releases and updates

The release workflow publishes a native Linux amd64 scorer and its updater in
the public `ditto-subnet-stack:generator-scorer-stable-1` descriptor channel.
Publication follows the exact-source release gates. The channel is discovery
only: the host resolves one immutable digest, verifies its keyless Cosign
signature against this repository's `release.yml` on `main`, and checks its
protocol, embedded revision and case bound before touching the running service.
It also pre-pulls the binary's digest-pinned generator sandbox images.

The Ansible role installs `ditto-generator-scorer-auto-update.timer`, polling
once a minute with up to 15 seconds of jitter. No GitHub runner connects to the
VM, and the updater never loads the scorer bearer. The first supervised
converge must pin `generator_scorer_revision` to a full released commit SHA;
it installs the drain-capable binary, root-owned release directories and timer.
Subsequent releases activate without another Ansible run.

Activation switches `/opt/ditto-generator-scorer/current` atomically, then sends
SIGUSR1 to the scorer's main process. It finishes the current lease and exits
before claiming more work. Systemd starts the candidate through `current`.
Adoption requires the candidate executable to stay active with the same PID
for ten seconds. The old process has up to 70 minutes to drain; after it exits,
the candidate has one minute to start. A failed adoption restores `last-good`
and requests another graceful restart. The exact failed digest is suppressed
for up to 24 hours, until a new descriptor is published, or until an operator removes
`/opt/ditto-generator-scorer/failed-candidate`. The updater retains prior releases and refreshes
itself only after successful adoption; an interrupted update keeps `last-good`
available for the next attempt.

Inspect with `systemctl status ditto-generator-scorer-auto-update.timer`,
`journalctl -u ditto-generator-scorer-auto-update`, and
`/opt/ditto-generator-scorer/current/challenge-scorer version`. Confirm the
running executable through `/proc/<MainPID>/exe`; a symlink target alone is
not proof of adoption. To hold or roll back, stop the update timer, point
`current` at a retained prior release under `releases/`, and send SIGUSR1 to the scorer.
SIGTERM remains immediate cancellation and is not used by the updater.

`cmd/challenge-scorer` does the generator competition's CPU-only work. It
builds generator source in a no-network sandbox and runs it on seeds. It never
talks to a model, and in Platform mode it never runs an agent: validators run
round datasets on screened agent images through the ordinary ticket and
inference path (`generator-round-v1` on `/v2/score`, see
`cmd/dittobench-api/generator_round.go`).

- `genrun` (in `research/dittobench-datagen`) owns the generator half: build,
  run, validate, and check determinism.
- `internal/challengerun` executes one pack against one ready harness. It uses
  the scored path's runner (wave barrier, `SeedForVersion`,
  `RunCaseWithTelemetry`) and grades with `scorer.GradeMemory` at the pack's
  pinned grading bench version.

The contract, sandbox limits, validation rules, and scoring formula are in
`research/dittobench-datagen/docs/generator-competition.md`.

## Sandbox

`serve` uses `genrun.Docker`, so the scorer host needs a Docker daemon. Builds
run in the pinned `golang:1.27.3-bookworm` image. Generation runs in pinned
distroless `static`. Both run with no network, a read-only root filesystem, all
capabilities dropped, an unprivileged user, and memory, CPU, process, and
wall-time limits.

A timed-out container is killed by name, so no container outlives its run.
Docker CLI failures (exit 125–127, daemon errors) are sandbox failures. They are
never reported to Platform as the generator's fault. The scorer posts them to
`POST /api/v1/generator-scorer/fault` with `error_kind=sandbox`.

A lease this build cannot serve is a fault too, never a generator failure. For
example, a stale scorer may be asked for more cases than its
`challenge.MaxCases`. `genrun.Evaluate` checks every request before it builds
anything and returns `genrun.ErrInvalidRequest`. The scorer then posts
`error_kind=unsupported_request`.

Platform handles a fault in one of these ways, and never rejects a generator:

- A qualification run is abandoned (`scorer_<kind>`). The submission stays
  active and backs off for `retry_backoff_seconds`.
- An unsupported round is voided with an `infra:` reason.
- A sandbox failure on a round releases the lease for an immediate re-lease.
  `MAX_GENERATION_ATTEMPTS` still bounds the retries.

Older scorers wrote request refusals (`request cases must be in 8..64`) into
seed and column errors. Platform still recognises those messages as the
scorer's fault.

Every work poll (`/work`, `/round-work` and `/dueling-work`) also advertises
this build's capability as query parameters: `scorer_id` (`-scorer-id`,
default the hostname), `scorer_version`, `min_cases` and `max_cases`
(`challenge.MinCases`/`MaxCases`), `grading_bench_versions` (the pack grading
policies this build requests and grades, `challenge.SupportedGradingBenchVersions`,
as `13,14`), `dueling_work_kinds` (`slice,battery` from `challenge-scorer/7`),
`sandbox` and, when the binary knows its commit, `build_revision`. Platform records the latest per scorer and uses it before
any fault can happen:

- it leases no qualification whose `cases_per_seed` the polling scorer refuses;
- it voids with an `infra:` reason a round the scorer cannot generate, at lease
  (the polling scorer) and at open (the live scorer);
- it refuses a generator settings write whose `cases_per_seed` the live scorer
  refuses, unless the operator passes `override_scorer_capability`;
- it opens a round at bench version N only when the live scorer lists N, voids
  with an `infra:` reason a round at N leased by a scorer that does not list
  it, and requests a qualification at the newest round contract the polling
  scorer lists.

The grading versions are explicit, the way a validator advertises
`generator-round-bench-<n>`: Platform never infers them from `scorer_version`.
A scorer that sends no `grading_bench_versions` (`challenge-scorer/5` and
older) is treated as v13-only. `challenge-scorer/6` is the first build that
sends them.

Backroom `get_generator_scorer` shows each scorer's version, bounds, grading
versions (advertised and effective), build and last poll. A scorer that
predates advertising sends nothing and is not gated on case bounds.

`-unsafe-local-sandbox` builds and runs on the host with no isolation. It
exists for local stacks only.

## Wire hygiene

Case, pair, session, and user IDs are replaced with per-run opaque IDs keyed by
a random nonce. A harness cannot recognize a generator's ID scheme. Two runs
look unrelated. A harness reused across packs answers each run from that run's
own memory graph.

A case whose `/run` fails is reported as `fault` and excluded from scoring. A
harness that never becomes healthy, or that rejects a seeding wave, faults
every case. Scorer-side failures therefore cannot produce "the champion failed
this case".

## Local rehearsal

```bash
go run ./cmd/challenge-scorer run -dir ../../miners/dittobench-generator-starter-kit \
  -hotkey 5Grw... -seeds 2 -cases 80 -sandbox docker \
  -agent mine=http://127.0.0.1:7070,owner=me \
  -agent baseline=http://127.0.0.1:7071,owner=ops,champion \
  -agent other=http://127.0.0.1:7072,owner=other \
  -generator-owner me -out run.json
```

Generators build and run in the Docker sandbox by default; `-sandbox local`
runs the generator's code directly on your machine, so use it only for your own
source. `run` rejects two `-agent` flags with the same id. It prints the run
score with the default parameters. It writes the full
verdict file: a `challenge.RunInput`, the params, and the `challenge.RunScore`.
Use `-archive` to rehearse an exact upload, or `-pack` to run a single pack file
instead of a generator.

## Shadow scorer

```bash
DITTO_GENERATOR_SCORER_TOKEN=... go run ./cmd/challenge-scorer serve \
  -platform https://platform.example
```

`-platform` must be an `https://` URL, because the scorer token is a bearer
credential. Use `-insecure-platform` only for a local stack. `-once` processes
at most one item of each kind and exits nonzero if any cycle failed.

Each `serve` cycle does at most one round work item, then at most one dueling
work item, then at most one qualification. Round work goes first because it
gates the validators and the ledger. Dueling slices come next because a v16+
agent lease waits (503 with `Retry-After`) until its slice is ready.

**Rounds** (`GET /api/v1/generator-scorer/round-work`):

- `kind=generate`: the work carries the round seed, the case count, and every
  frozen column's exact source.
  1. The scorer refuses any source whose SHA-256 differs from the column pin.
  2. It builds and runs each column generator on
     `challenge.RoundColumnSeed(seed, column)` and validates the pack.
  3. It encodes the valid packs with `challenge.EncodeRoundDataset`.
  4. It posts the column results and the dataset to `POST /round-results`.
- `kind=score`: the work carries the round matrix. That is each column's case
  refs and owner, every panel agent's validator results, and the fingerprints
  earlier rounds claimed. The scorer applies `challenge.ScoreRound` and posts
  column scores and pairwise-neutral agent standings to `POST /round-score`.

**Dueling** (`GET /api/v1/generator-scorer/dueling-work`, bench v16+,
[dueling-bench.md](../../../research/dittobench-datagen/docs/dueling-bench.md)
sections 2 and 7.4). Platform answers `{"work": null}` when idle. A Platform
that predates dueling work answers 404, which the scorer treats as idle
without logging. Builds are cached by `source_sha256` across leases (up to 24
generators, least recently used out), so a season's binaries are built once
and a slice is five sandboxed runs. A build's compile or archive failure is
cached too. A build timeout is reported as a `sandbox` fault, never as the
generator's failure: every dueling generator already built inside the same
limit to qualify.

- `kind=battery`: the season qualification battery.
  `{kind, lease_id, season_id, bench_version, cases, seeds: [hex],
  candidates: [{submission_id, miner_hotkey, owner_key, source_sha256,
  source_base64}]}`.
  1. Every request (`challenge.NewGenerateRequestFor(seed, hotkey, cases,
     bench_version)`, so the season's two-policy window) is checked before
     anything is built.
  2. Each candidate is built once and run on every seed. Every pack must pass
     `challenge.ValidateGenerated`, and all of them must declare the same
     grading policy inside the window.
  3. Seed 0 is regenerated and must produce identical pack bytes.
  4. The scorer posts `POST /api/v1/generator-scorer/dueling-battery`
     `{lease_id, season_id, scorer_version, candidates: [{submission_id, ok,
     grading_bench_version, error}]}`. `grading_bench_version` is present only
     when `ok`; `error` names the first failure (`build: ...`, `seed <i>: ...`
     or `determinism: ...`).
- `kind=slice`: one agent run's generator slice.
  `{kind, lease_id, preparation_id, season_id, season_index, bench_version,
  run_seed, cases_per_column: 35, columns: [{column_id, submission_id,
  miner_hotkey, owner_key, source_sha256, source_base64, grading_bench_version,
  slice_seed, public_default}], fallback: {submission_id, miner_hotkey,
  source_sha256, source_base64}}`.
  1. The scorer refuses, as an `unsupported_request` fault, a slice it cannot
     produce: a bench version below 16, a column set other than `c0..c4`, a
     case count other than 35, a pinned policy outside the window, or a miner
     column without an `owner_key`.
  2. Each column runs `challenge.SliceGenerateRequest` (its `slice_seed`, its
     hotkey, exactly its pinned policy) and the pack is validated.
  3. A miner column whose generator fails (build, crash, timeout, invalid
     pack) is regenerated with `fallback` on the same `slice_seed` and pinned
     policy, and recorded as substituted: `public_default: true`,
     `substituted: true`, the fallback's submission and hotkey, and an empty
     `owner_key`. A public default column is never substituted.
  4. The columns are encoded with `challenge.EncodeSliceDataset`. Each
     column's `tree_sha256` is the tree digest of the archive actually built;
     a lease that also pins `tree_sha256` must match it.
  5. The scorer posts `POST /api/v1/generator-scorer/dueling-slice`
     `{lease_id, preparation_id, scorer_version, columns: [{column_id, ok,
     substituted, error}], slice_sha256, slice_base64}`. `ok` means the slice
     holds a valid pack for the column (the pinned generator's, or the
     fallback's when `substituted`). `error` is the pinned generator's failure
     and is set whenever a column was substituted or failed, so Platform can
     exclude that generator from the next season. When any column has no pack
     (no fallback, or the fallback failed too) or encoding fails, the slice
     fields are omitted and Platform fails the preparation.

A sandbox failure or a request this build refuses is posted to
`POST /api/v1/generator-scorer/fault` with `work: "dueling"`, the `lease_id`,
`season_id` and, for a slice, `preparation_id`. A source that does not hash to
its pin (or a pinned `tree_sha256` the archive does not match) is never run and is reported as an `unsupported_request` fault.

**Qualification** (`GET /api/v1/generator-scorer/work`):

1. The work carries a new submission's exact archive, fresh secret seeds, the
   current probe seed, and the case count.
2. The scorer builds the generator.
3. It runs each seed and the probe, then re-runs the first seed to check
   determinism.
4. It posts the evaluation to `POST /results`. Platform marks the generator
   qualified or rejects it, which includes the copy check.

The token is read from the environment, is sent only as a Bearer header, and is
never logged.

## Reproducing a dueling slice

A slice becomes reproducible once its season is revealed (secret and anchor
block public) and each pinned generator's source is published
(dueling-bench.md "Delayed reproducibility"). `reproduce-slice` rebuilds it
byte for byte and prints its SHA-256, which must equal the `slice_sha256` on
the score row:

```bash
go run ./cmd/challenge-scorer reproduce-slice \
  -season-secret <64 hex> -season-index 26119 -anchor-block-hash 0x<64 hex> \
  -run-seed 123 -bench-version 16 -pins pins.json \
  -source c0=gen-a.tar.gz -source c1=default.tar.gz -source c2=./gen-b ... \
  -expect-sha256 <slice_sha256> -out slice.json
```

`pins.json` holds `season_id` and each column's pin: `column_id`,
`submission_id`, `owner_key`, `miner_hotkey`, `tree_sha256`,
`grading_bench_version`, `public_default` and `substituted`. A revealed slice
file has this shape and works as pins; its packs are ignored. The command
derives every column's `slice_seed` from the secret, anchor and run seed and
refuses a pin whose `slice_seed` differs. It logs the season commitment, so
you can compare it with the published one. A substituted column's `-source` is
the public default generator's archive. A `-source` directory is packed like an
upload, and its tree digest must match the pinned `tree_sha256`. Builds run in
the Docker sandbox unless `-sandbox local`.

## Production deployment

The scorer runs on its own private VM, `ditto-generator-scorer-prod`, and
nowhere else, because it executes untrusted miner code. The Platform,
screener, and validator hosts never build a generator.

1. Protected Terraform apply with `enable_generator_scorer = true`
   (`infra/terraform/stacks/gcp-platform/generator-scorer.tf`). This creates
   the VM, its identity, and the `generator-scorer-token-prod` secret (which
   exists even with the flag off, since Platform names it). Both
   the scorer identity and the Platform identity can read the secret.
2. Add one secret version without printing it:
   `openssl rand -hex 32 | gcloud secrets versions add generator-scorer-token-prod --data-file=- --project=ditto-app-dev`.
3. Converge Platform (`infra/ansible/playbooks/gcp-platform-app.yml`) so that
   `DITTO_GENERATOR_SCORER_TOKEN` is rendered, then restart the API.
4. Converge the scorer at the released commit:
   `infra/ansible/playbooks/gcp-generator-scorer.yml -e generator_scorer_revision=<sha>`.
   This builds `challenge-scorer` with the base role's pinned Go toolchain,
   pre-pulls the pinned sandbox images, and runs `challenge-scorer serve`
   under systemd. The unit gets Docker access and only its own bearer.

The unit sets `TMPDIR` under `/var/lib/ditto-generator-scorer`, because the
sandbox bind-mounts its build directories. `PrivateTmp` would hide them from
dockerd.

## AWS deployment

The same host can run on EC2 instead. Nothing about the scorer changes: the
Ansible role, the systemd unit, the sandbox images, and the Platform bearer are
the same. Only placement and how the host reaches its bearer differ.

| | GCE (today) | AWS |
|---|---|---|
| Terraform | `gcp-platform/generator-scorer.tf` | `infra/terraform/stacks/aws-generator-scorer` |
| Compute | `modules/compute/gcp`, `app-standard` (e2-standard-4) | `modules/compute/aws`, `app-small` (t3.medium, unlimited credits) |
| Access | IAP + OS Login SSH | SSM Session Manager; no ingress at all |
| Inventory | `inventory/gcp.yml` | `inventory/aws.yml` (`amazon.aws.aws_ec2` + `aws_ssm`) |
| Bearer source | `gcloud` (attached service account) | `aws-wif` (instance role federated into GCP) |

Both providers share the logical sizes in
`infra/terraform/modules/compute/interface.md`. A third place (for example a
Hetzner node) converges with `generator_scorer_token_source=file` and a
root-only `/etc/ditto-generator-scorer/platform-token`.

Why these choices:

- **t3.medium, x86.** One sandboxed build is capped at 2 GiB, a generator run
  at 1 GiB, plus Docker, so 2 GiB (t3.small) is too small. Graviton is out:
  Go fuses FMA on arm64, which can change generator output bytes against
  miners' x86 machines and break the determinism check and the fingerprints.
  `unlimited` credits keep a long build from being throttled to baseline and
  timing out as if the generator were at fault.
- **Debian 13**, as on GCE. Its AMI has no SSM agent, so first-boot user data
  installs one pinned, checksum-verified `amazon-ssm-agent` release.
- **Dedicated VPC, public IPv4, no NAT.** The account's default VPC is shared
  and editable out of band; a NAT gateway costs more than the host. The
  security group has no ingress rules and allows only TCP 80/443 out.
  IMDSv2 is required with hop limit 1, so sandbox containers cannot reach the
  instance credentials. The root volume is encrypted gp3.
- **The bearer stays in GCP Secret Manager**, because Platform reads it there.
  The `generator-scorer-aws` Workload Identity pool trusts one AWS account and
  accepts only sessions of the `ditto-generator-scorer` instance role. That
  principal set can read `generator-scorer-token-prod` and nothing else. There
  is no copied secret and no key: gcloud signs an STS `GetCallerIdentity`
  request with the instance credentials and exchanges it for a federated
  token. A copy in AWS Secrets Manager would need its own rotation and could
  drift from Platform's value.

### One-time AWS bootstrap

An account administrator runs the commands in
[`infra/docs/ci-bootstrap.md`](../../../infra/docs/ci-bootstrap.md#aws-roots)
once. They create the S3 state bucket, the GitHub OIDC provider, and the plan
and apply roles that `infra-plan-apply.yml` assumes. The same administrator
root also creates the scorer's `ditto-generator-scorer` instance role and
profile when `enable_generator_scorer_identity = true` (cutover step 2), so the
CI apply role, which GCP federation would otherwise let mint the bearer's
identity, can only read and pass it. Nothing else is created out of band.

### Cutover runbook

Merging the Terraform creates nothing. `enable_generator_scorer_identity`
(aws-bootstrap), `enable_generator_scorer` (AWS root) and
`generator_scorer_aws_account_id` (gcp-platform) default to off. The GCE VM
keeps scoring until step 7.

1. **Enable both sides in one PR.** In
   `infra/terraform/stacks/gcp-platform/prod.auto.tfvars` set
   `generator_scorer_aws_account_id = "339713011188"`. Add
   `infra/terraform/stacks/aws-generator-scorer/prod.auto.tfvars` with
   `enable_generator_scorer = true`.
2. **Bootstrap the instance identity.** An SSO administrator re-applies
   `aws-bootstrap` with `-var enable_generator_scorer_identity=true`
   ([`ci-bootstrap.md`](../../../infra/docs/ci-bootstrap.md#generator-scorer-identity)).
   Expect only the `ditto-generator-scorer` role, its
   `AmazonSSMManagedInstanceCore` attachment, the instance profile, and any
   pending in-place update to the two CI role policies. Keep the value in
   every later bootstrap plan.
3. **Protected plan/apply, `root=gcp-platform`.** The saved plan should create
   exactly the pool, its `aws-ec2` provider, and one secret IAM member. The pool
   admin grant already exists while X.509 federation is on. Reject any other
   change.
4. **Protected plan/apply, `root=aws-generator-scorer`.** Expect only creates:
   the VPC and its subnet, gateway, routes, egress-only security group and its
   two rules, the transfer bucket and its settings, and one `t3.medium`. No IAM
   resource appears: the root reads the bootstrap profile and fails the plan
   unless it wraps `ditto-generator-scorer`. Wait for the instance to appear as
   an SSM managed node (`aws ssm describe-instance-information`).
5. **Converge.** From a controller with an AWS session for the account,
   `boto3`/`botocore`, the `session-manager-plugin`, and
   `ansible-galaxy collection install -r infra/ansible/requirements.yml`:

   ```bash
   ansible-inventory -i infra/ansible/inventory/aws.yml --graph   # role_generator_scorer has the host
   ansible-playbook -i infra/ansible/inventory/aws.yml \
     infra/ansible/playbooks/aws-generator-scorer.yml \
     -e generator_scorer_revision=<released commit running on GCE>
   ```

   Use the commit the GCE host runs, so both hosts score identically. The
   converge fails closed if the federated read returns no usable bearer.
6. **Verify a qualification run on AWS.** Both scorers now poll with the same
   bearer. Platform leases each item to one of them, so running both is safe.
   On the AWS host (`aws ssm start-session --target <instance-id>`), watch
   `journalctl -u ditto-generator-scorer -f` until it leases and posts a
   qualification run. If none arrives, re-qualify an active generator through
   Backroom (`rescore_generator_submission`). Confirm the run with
   `get_generator_run`: finished, determinism check passed, and the same
   qualified/rejected outcome as the GCE host's last run of that source (a
   re-qualification draws fresh seeds, so compare outcomes, not bytes).
7. **Stop the GCE unit.** On `ditto-generator-scorer-prod`, run
   `sudo systemctl disable --now ditto-generator-scorer`. Watch the AWS journal
   through at least one more qualification and, if one is open, one round
   lease.
8. **Retire the GCE VM in two protected phases.** The compute module keeps
   `prevent_destroy`, so a flag flip alone cannot delete it.
   1. Set `generator_scorer_gce_deletion_protection = false`. The saved plan
      must contain only that in-place update.
   2. In a separate change, remove the `generator_scorer_vm` module, the
      `generator_scorer` service account, and its secret grant from
      `generator-scorer.tf`. Keep the secret and Platform's grant. Plan with
      `targets` naming those addresses and confirm the plan contains only
      their deletion.

Rollback before step 8: `sudo systemctl enable --now ditto-generator-scorer`
on the GCE VM, then stop the AWS unit or set `enable_generator_scorer = false`.
Disabling the AWS root destroys the instance only after its
`deletion_protection` is set false in a reviewed plan.
