# Production GCP validator and hotkey cutover

This runbook moves the operator-owned SN118 validator to the private
`ditto-validator-prod` GCE host. It deliberately separates four authorities:

1. Terraform creates the host, dedicated runtime identity, IAP/OS Login policy,
   and an empty Secret Manager container.
2. Peyton or Omar uses a two-phase disposable GCE admin to generate one hotkey
   and stream only its mnemonic into that container. Terraform, GitHub, and the
   operator's development machine never see the value.
3. The offline coldkey workstation submits the SN118-only hotkey swap. The
   coldkey, its mnemonic, and its password never enter GCP or this repository.
4. The GCP host runs only a Cosign-authenticated, digest-pinned managed stack.

Every sudo user on the VM can read the unencrypted hotkey file that Pylon must
use to sign. `validator_prod_operators` is therefore restricted to Peyton and
Omar. Review it like a key-custody list, and never grant generic debug or
deployment identities access to the host.

## 1. Merge and provision the inert host

Merge the reviewed change before touching infrastructure. Run the protected
`gcp-platform` plan from the exact current `main` SHA and inspect the complete
plan. It must create, not replace:

- `ditto-validator-prod`, private IP only, Shielded VM Secure Boot/vTPM;
- its dedicated `10.31.0.0/24` subnet and an egress deny to the Platform subnet;
- `ditto-validator-prod@ditto-app-dev.iam.gserviceaccount.com`;
- the empty `validator-prod-hotkey-mnemonic` secret;
- the inert firewall/custom-role definition for a disposable hotkey generator,
  with no generator VM, disk, service account, or IAM binding while its phase
  remains `absent`;
- only logging/monitoring and VM secret-accessor grants;
- Peyton/Omar host access and secret-scoped lifecycle management without
  permission to read or destroy the secret payload.

Apply only that saved binary plan through the protected `infra-apply`
environment. A merge alone creates nothing. After apply:

```sh
gcloud compute instances describe ditto-validator-prod \
  --project ditto-app-dev --zone us-central1-a \
  --format='yaml(name,status,networkInterfaces,serviceAccounts,shieldedInstanceConfig,deletionProtection)'
```

Stop if the instance has an external IP, uses a different service account, or
Secure Boot/vTPM/integrity monitoring is not enabled. From the validator host,
also prove that a TCP connection to `ditto-pg-platform` on 5432 is rejected;
do not proceed if the validator can reach the production database network.

## 2. Generate the GCP-held hotkey on a disposable admin

Do not run the generator on an everyday development machine or on the coldkey
workstation. The reviewed Terraform is the reusable template: it creates one
private, Shielded, IAP-only admin and auto-delete boot disk for this ceremony,
then removes the VM, disk, service account, and IAM bindings. There is no idle
generator compute or disk after teardown.

Generation has two protected applies so an internet-connected dependency
bootstrap never overlaps Secret Manager write authority:

1. Run `infra-plan-apply.yml` for `gcp-platform` with
   `validator_hotkey_admin_phase=bootstrap` and an empty revision input. Record
   the plan SHA as `HOTKEY_ADMIN_REVISION`, review the complete plan, and apply
   its saved binary plan. The VM has NAT egress for the exact canonical-main
   checkout and frozen dependencies, but no permission on the mnemonic secret.
2. Wait for `/var/lib/ditto-hotkey-admin/ready`, then run another protected plan
   with `validator_hotkey_admin_phase=armed` and
   `validator_hotkey_admin_revision=HOTKEY_ADMIN_REVISION`. The arm plan must
   update the existing instance in place: remove only the bootstrap network tag,
   add the armed tag, and attach the exact-secret add-only IAM binding. Stop if
   it replaces the VM, disk, template, service account, or source revision. If
   `main` advanced after bootstrap, tear down and restart the ceremony instead
   of arming an older checkout.

After the arm apply, require a private IP, no access config, the armed tag, and
no bootstrap tag:

```sh
gcloud compute instances describe ditto-validator-hotkey-admin \
  --project ditto-app-dev --zone us-central1-a \
  --format='yaml(name,status,tags,networkInterfaces)'
gcloud compute ssh ditto-validator-hotkey-admin \
  --project ditto-app-dev --zone us-central1-a --tunnel-through-iap
```

On the disposable admin, confirm arbitrary internet egress is denied while
Secret Manager metadata calls still work, then generate exactly once with both
custodians watching:

```sh
sudo test -f /var/lib/ditto-hotkey-admin/ready
if curl --fail --silent --max-time 5 https://github.com >/dev/null; then
  echo 'ERROR: armed generator still has general internet egress' >&2
  exit 1
fi
sudo gcloud secrets describe validator-prod-hotkey-mnemonic \
  --project ditto-app-dev --format='value(name)'
sudo /usr/local/sbin/generate-validator-hotkey
sudo cat /var/lib/ditto-hotkey-admin/result.env
```

The helper is locally locked, refuses any secret with an existing version, and
persists only the public SS58 address and numeric version ID so an SSH
disconnect does not require revealing the mnemonic. Record `validator_hotkey`
as `NEW_HOTKEY` and `secret_version` as `HOTKEY_SECRET_VERSION`. Do not copy the
mnemonic to a password manager, shell variable, ticket, chat, or local file;
Secret Manager is the recovery copy.

Power off the admin, then immediately run and apply a reviewed `gcp-platform`
plan with `validator_hotkey_admin_phase=absent` and an empty revision input.
Require the plan to delete the VM, auto-delete boot disk, instance template,
service account, secret binding, and per-instance human access. Verify teardown
and exactly one recovery version before continuing:

```sh
sudo poweroff
```

After SSH disconnects, run and apply the reviewed absent-phase plan from the
protected workflow. Then verify from the authenticated operator terminal:

```sh
! gcloud compute instances describe ditto-validator-hotkey-admin \
  --project ditto-app-dev --zone us-central1-a
! gcloud iam service-accounts describe \
  validator-hotkey-admin@ditto-app-dev.iam.gserviceaccount.com \
  --project ditto-app-dev
gcloud secrets versions list --secret validator-prod-hotkey-mnemonic \
  --project ditto-app-dev --format='value(name,state)'
```

Do not destroy the Secret Manager version.

## 3. Converge and verify without activation

Use the merge SHA, never a branch or tag:

```sh
export GCP_OSLOGIN_USER=YOUR_OS_LOGIN_USER
export VALIDATOR_STACK_REVISION=MERGED_MAIN_SHA
export VALIDATOR_PROD_HOTKEY=NEW_HOTKEY
export VALIDATOR_PROD_HOTKEY_SECRET_VERSION=HOTKEY_SECRET_VERSION
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-validator-prod.yml \
  --limit ditto-validator-prod
```

The play installs Docker and checksum-pinned Cosign, checks out exactly that
SHA, materializes an unencrypted hotkey wallet, verifies that its public address
equals `NEW_HOTKEY`, generates host-local Pylon/control tokens, and validates
Compose. It does not start a validator.

Disable the Secret Manager version, then run the same play again. The second
converge must succeed from the existing wallet without accessing the disabled
seed:

```sh
gcloud secrets versions disable HOTKEY_SECRET_VERSION \
  --secret validator-prod-hotkey-mnemonic --project ditto-app-dev
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-validator-prod.yml \
  --limit ditto-validator-prod
```

If recovery is ever required, first disable both updater timers, drain the
validator if it can still run, stop every validator-stack container, and verify
that none remain running. Only then may Peyton or Omar re-enable that exact
version for one supervised materialization. Disable it again and complete a
verify-only converge before any stack container is restarted. The custodian
role cannot read or destroy the payload; the stopped VM service account reads
it during the supervised play.

## 4. Authenticate and pre-pull the release

On `ditto-validator-prod` as `deploy`, resolve the stable channel once to an
immutable digest, then prepare that exact release. `prepare` verifies the
GitHub Actions keyless Cosign identity, descriptor contract, component labels,
and every image without starting a service.
`prepare` also verifies the validator image's fail-closed bootstrap-readiness
capability label. This intentionally rejects releases that predate this change;
wait for the release built from the merged PR instead of preparing an older
`compat-2` digest.

```sh
cd /opt/ditto/validator-stack/src
docker pull ghcr.io/ditto-assistant/ditto-subnet-stack:compat-2
STACK_DIGEST="$(docker image inspect \
  --format '{{ range .RepoDigests }}{{ println . }}{{ end }}' \
  ghcr.io/ditto-assistant/ditto-subnet-stack:compat-2 | \
  awk '/^ghcr.io\/ditto-assistant\/ditto-subnet-stack@sha256:/ { print; exit }')"
test -n "$STACK_DIGEST"
./scripts/validator-stack-auto-update.sh prepare "$STACK_DIGEST"
./scripts/validator-stack-auto-update.sh status
```

Record `STACK_DIGEST` in the cutover record. Do not substitute the mutable
`compat-2` tag in a later command.

## 5. Prepare the coldkey workstation

Use a separate isolated workstation holding the new owner's coldkey. The
previous operator's validator hotkey is compromised because another party may
retain its file or seed. Never copy it to GCP, and do not treat coldkey
ownership as revoking its signing authority. The destination is the raw
`NEW_HOTKEY` SS58 address printed by the GCP generator; its mnemonic never
appears in operator output and is retained only in Secret Manager.

Do not assume the repository's pinned CLI implements the documented command.
The deprecated Homebrew `btcli` formula currently lacks the `tx` command; a
routine `brew upgrade btcli` does not fix that product-line mismatch. On the
dedicated coldkey workstation, replace it with Homebrew's `bittensor` formula
before restoring or importing the coldkey when possible:

```sh
brew uninstall btcli
brew install bittensor
```

Then require all of the following to succeed before scheduling any drain, and
record the version and executable checksum in the cutover record:

```sh
command -v btcli
btcli --version
btcli tx swap-hotkey --help
openssl dgst -sha256 "$(command -v btcli)"
```

Compare the command surface with the
[official hotkey-swap documentation](https://www.bittensor.com/docs/tx/swap-hotkey).

Before continuing, query Finney and record evidence that:

- the old hotkey owns the expected SN118 UID and validator permit;
- `NEW_HOTKEY` is not registered on SN118 or another subnet being swapped;
- the coldkey has enough free balance for the displayed fee;
- the previous operator's validator process is identified and ready to stop at
  cutover;
- no other live process intentionally depends on the old hotkey.

Before draining anything, preview the SN118-only swap:

```sh
btcli tx swap-hotkey \
  --hotkey OLD_HOTKEY_SS58 \
  --new-hotkey NEW_HOTKEY \
  --netuid 118 \
  -w NEW_COLDKEY_WALLET \
  --dry-run
```

Require the preview to name exactly the expected old and new SS58 addresses,
coldkey, and netuid 118. The documented per-subnet recycle amount is 0.001 TAO;
confirm the live amount shown by `btcli`. A `(netuid, coldkey)` pair has a
7,200-block (approximately one-day) swap cooldown. Stop if any field differs or
the destination is already registered. Both custodians must review and sign
the recorded dry-run evidence before the maintenance window starts.

## 6. Drain, swap SN118, and bootstrap GCP

Drain the previous operator's validator and wait for a fresh Platform-accepted
`drained` state. Do not interrupt any ordinary or confirmation benchmark. Stop
the old validator process only after every active lease and weight update is
finished, and verify it remains offline throughout the swap and GCP bootstrap.

On the coldkey workstation, submit the command that both custodians previewed,
with only `--dry-run` removed:

```sh
btcli tx swap-hotkey \
  --hotkey OLD_HOTKEY_SS58 \
  --new-hotkey NEW_HOTKEY \
  --netuid 118 \
  -w NEW_COLDKEY_WALLET
```

Do not omit `--netuid` or broaden this to an all-subnet swap. Capture the
extrinsic identifier. The old hotkey stops earning immediately after the swap.
Query the chain again and require the same SN118 UID/coldkey ownership,
registration, stake, and validator permit under `NEW_HOTKEY`, with the old
hotkey absent from SN118.

Then, on the GCP host, use the exact digest prepared earlier:

```sh
cd /opt/ditto/validator-stack/src
./scripts/validator-stack-auto-update.sh bootstrap "$STACK_DIGEST"
sudo DITTO_VALIDATOR_UPDATE_USER=deploy \
  ./scripts/install-validator-stack-auto-update.sh
./scripts/validator-stack-auto-update.sh status
```

Bootstrap starts the stack drained, then requires a fresh accepted signed
heartbeat, a served scorer probe, and healthy/ready required components
(including Pylon). It records the managed descriptor and only then resumes
ticket intake. If the hotkey is rejected or any functional check fails, the new
validator remains drained, unmanaged, and retryable with the same prepared
digest.

## 7. Acceptance and rollback boundary

Require all of the following before decommissioning the old host:

- the public validators endpoint lists `NEW_HOTKEY` online and healthy;
- `stack.mode` is `managed` and its descriptor equals `STACK_DIGEST`;
- Pylon, sandbox Docker, DittoBench, and validator health are all healthy;
- one signed score is accepted from `NEW_HOTKEY`;
- one weight submission advances the on-chain last-update block;
- updater and prefetch timers are active, with no failed candidate;
- disk remains below the configured 90% admission ceiling under a real run.

Before the on-chain swap, rollback is simply to keep GCP inert; only the old
validator may be resumed, after confirming that doing so is still acceptable
despite the compromised hotkey. After the swap, never restart the previous
operator's hotkey process: it no longer owns the SN118 registration and remains
compromised. Prefer repairing the safely drained GCP stack. A reverse hotkey
swap is a second coldkey-signed on-chain transaction subject to the cooldown.
It must target another newly generated clean hotkey—never the compromised old
hotkey. Verify its live availability, scope, and recycle amount before treating
it as a rollback. Never destroy the Secret Manager version or the old host
until the GCP canary and an operator-agreed observation window are complete.
