# Production GCP validator and hotkey cutover

This runbook moves the operator-owned SN118 validator to the private
`ditto-validator-prod` GCE host. It deliberately separates four authorities:

1. Terraform creates the host, dedicated runtime identity, IAP/OS Login policy,
   and an empty Secret Manager container.
2. Peyton or Omar generates one hotkey and streams only its mnemonic into that
   container. Terraform and GitHub never see the value.
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

## 2. Generate the GCP-held hotkey

One named custodian, witnessed by the other, must use the exact merged checkout
and its locked Bittensor dependency. The helper refuses any secret that already
has a version and prints only the public SS58 address plus the Secret Manager
version name. It never prints the mnemonic.

```sh
uv run --frozen python infra/ansible/scripts/generate_validator_hotkey.py \
  --project ditto-app-dev \
  --secret validator-prod-hotkey-mnemonic \
  --confirm 'CREATE GCP VALIDATOR HOTKEY'
```

Record `validator_hotkey` as `NEW_HOTKEY` and the numeric tail of
`secret_version` as `HOTKEY_SECRET_VERSION`. Do not copy the mnemonic to a
password manager, shell variable, ticket, chat, or local file; Secret Manager
is the recovery copy. Do not destroy the version.

## 3. Converge and verify without activation

Use the merge SHA, never a branch or tag:

```sh
export GCP_OSLOGIN_USER=YOUR_OS_LOGIN_USER
export VALIDATOR_STACK_REVISION=MERGED_MAIN_SHA
export VALIDATOR_PROD_HOTKEY=NEW_HOTKEY
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

Use an isolated workstation holding the new owner's coldkey. Seby's old
validator hotkey is compromised because he may retain its file or seed. Never
copy it to GCP, and do not treat coldkey ownership as revoking its signing
authority. The destination is the raw `NEW_HOTKEY` SS58 address printed by the
GCP generator; its mnemonic never leaves Secret Manager.

Do not assume the repository's pinned CLI implements the documented command.
On the isolated coldkey workstation, require all of the following to succeed
before scheduling any drain, and record the version and executable checksum in
the cutover record:

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
- Seby's old validator process is identified and ready to stop at cutover;
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

Drain Seby's old validator and wait for a fresh Platform-accepted `drained`
state. Do not interrupt any ordinary or confirmation benchmark. Stop the old
validator process only after every active lease and weight update is finished,
and verify it remains offline throughout the swap and GCP bootstrap.

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
despite the compromised hotkey. After the swap, never restart Seby's old
hotkey process: it no longer owns the SN118 registration and remains
compromised. Prefer repairing the safely drained GCP stack. A reverse hotkey
swap is a second coldkey-signed on-chain transaction subject to the cooldown.
It must target another newly generated clean hotkey—never Seby's compromised
old hotkey. Verify its live availability, scope, and recycle amount before
treating it as a rollback. Never destroy the Secret Manager version or the old
host until the GCP canary and an operator-agreed observation window are
complete.
