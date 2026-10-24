# Generator competition scorer

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
never reported to Platform as the generator's fault: the scorer logs them and
lets the lease expire.

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
  -hotkey 5Grw... -seeds 2 -cases 16 -sandbox docker \
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
at most one item of each kind and exits nonzero if either cycle failed.

Each `serve` cycle does at most one round work item, then at most one
qualification. Round work goes first because it gates the validators and the
ledger.

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
and apply roles that `infra-plan-apply.yml` assumes. Nothing else is created
out of band.

### Cutover runbook

Merging the Terraform creates nothing. `enable_generator_scorer` (AWS root) and
`generator_scorer_aws_account_id` (gcp-platform) default to off. The GCE VM
keeps scoring until step 6.

1. **Enable both sides in one PR.** In
   `infra/terraform/stacks/gcp-platform/prod.auto.tfvars` set
   `generator_scorer_aws_account_id = "339713011188"`. Add
   `infra/terraform/stacks/aws-generator-scorer/prod.auto.tfvars` with
   `enable_generator_scorer = true`.
2. **Protected plan/apply, `root=gcp-platform`.** The saved plan should create
   exactly the pool, its `aws-ec2` provider, and one secret IAM member. The pool
   admin grant already exists while X.509 federation is on. Reject any other
   change.
3. **Protected plan/apply, `root=aws-generator-scorer`.** Expect only creates:
   the VPC and its subnet, gateway, routes and groups, the role, the instance
   profile and SSM attachment, the transfer bucket and its settings, and one
   `t3.medium`. Wait for the instance to appear as an SSM managed node
   (`aws ssm describe-instance-information`).
4. **Converge.** From a controller with an AWS session for the account,
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
5. **Verify a qualification run on AWS.** Both scorers now poll with the same
   bearer. Platform leases each item to one of them, so running both is safe.
   On the AWS host (`aws ssm start-session --target <instance-id>`), watch
   `journalctl -u ditto-generator-scorer -f` until it leases and posts a
   qualification run. If none arrives, re-qualify an active generator through
   Backroom (`rescore_generator_submission`). Confirm the run with
   `get_generator_run`: finished, determinism check passed, and the same
   qualified/rejected outcome as the GCE host's last run of that source (a
   re-qualification draws fresh seeds, so compare outcomes, not bytes).
6. **Stop the GCE unit.** On `ditto-generator-scorer-prod`, run
   `sudo systemctl disable --now ditto-generator-scorer`. Watch the AWS journal
   through at least one more qualification and, if one is open, one round
   lease.
7. **Retire the GCE VM in two protected phases.** The compute module keeps
   `prevent_destroy`, so a flag flip alone cannot delete it.
   1. Set `generator_scorer_gce_deletion_protection = false`. The saved plan
      must contain only that in-place update.
   2. In a separate change, remove the `generator_scorer_vm` module, the
      `generator_scorer` service account, and its secret grant from
      `generator-scorer.tf`. Keep the secret and Platform's grant. Plan with
      `targets` naming those addresses and confirm the plan contains only
      their deletion.

Rollback before step 7: `sudo systemctl enable --now ditto-generator-scorer`
on the GCE VM, then stop the AWS unit or set `enable_generator_scorer = false`.
Disabling the AWS root destroys the instance only after its
`deletion_protection` is set false in a reviewed plan.
