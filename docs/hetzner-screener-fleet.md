# `subnet-screener-1` setup and operation

`subnet-screener-1` is the normal-load screener. Eight full workers share one
enrolled node identity, while Platform admits at most four disposable KVM
build/smoke guests and four source reviews at once on the 64 GB host. Different
submissions progress concurrently, but one submission is always ordered:

```text
static safety preflight -> build -> runtime smoke -> general source review -> verdict
```

GCE is overflow capacity for work that the primary node has not claimed. It is
not an automatic retry destination after a failed Hetzner build, smoke, or
review. The capacity controller starts GCE only when the primary heartbeat is
not ready or the unclaimed queue exceeds the audited backlog multiple.

## 0. Rehearse the host on disposable GCE

Before changing the Robot server, use the protected `Infrastructure plan or
apply` workflow with root `gcp-platform` and
`screener_fleet_dev_host_enabled=true`. This creates
`subnet-screener-dev-1`, an `n2-standard-16` Debian 12 host with 64 GB RAM,
private networking, IAP SSH, and nested KVM. The flag defaults to false and the
VM receives no screener identity, source-review key, or Platform secret.

The checked-in GCE group variables pin the same versioned Debian 12 image and
SHA-256 exercised during development. Run the preparation-only playbook through
the existing GCP dynamic inventory:

```bash
export GCP_OSLOGIN_USER="$(gcloud compute os-login describe-profile \
  --format='value(posixAccounts[0].username)')"
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-screener-fleet-dev.yml
```

The rehearsal installs and verifies KVM/libvirt, produces the digest-verified
guest base, and proves it installed no screener service. Re-run the protected
plan/apply with `screener_fleet_dev_host_enabled=false`, then verify the output
is empty and `subnet-screener-dev-1` no longer exists. The sealed state is
always absence.

## 1. Install Debian on the auction server

In Hetzner Robot, boot the rescue system, add the operator SSH key, then run
`installimage`. Select Debian 13, hostname `subnet-screener-1`, and RAID 1 across
both NVMe drives. Keep the root filesystem large enough for image archives and
KVM overlays; do not expose libvirt or Docker ports.

Ghostty advertises `TERM=xterm-ghostty`, which Hetzner rescue's editor may not
recognize and can accidentally splice an error into `HOSTNAME` and `IMAGE`.
Normalize the terminal before starting:

```bash
export TERM=xterm-256color
installimage
```

Use both NVMes, `SWRAID 1`, `SWRAIDLEVEL 1`, and this simple layout:

```text
PART /boot/efi esp 256M
PART swap swap 32G
PART /boot ext3 1024M
PART / ext4 all
```

Before accepting the destructive confirmation, verify that `HOSTNAME` is
exactly `subnet-screener-1` and `IMAGE` is a real Debian `.tar.zst` path with no
terminal-error text.

Reboot and verify root SSH before continuing. The first Ansible converge moves
SSH access to the configured Ditto operator account and disables root login.

## 2. Prepare immutable inputs and one-time authority

Copy the public inventory example to the ignored inventory:

```bash
cp infra/ansible/inventory/hetzner-screener.example.yml \
  infra/ansible/inventory/hetzner-screener.yml
```

The public inventory already pins the versioned Debian 12 genericcloud image
exercised by the GCE rehearsal. If it changes, verify Debian's official checksum
manifest and update its exact URL and digest together. Put the actual Hetzner
Robot server ID, the exact 40-character public release commit, and a
digest-pinned submission-builder image in the private inventory;
mutable branches and image tags are rejected.

Read the current controller epoch with Backroom's `get_screener_capacity`, then
use `create_screener_bootstrap_grant` with its exact confirmation phrase to
create a single-use prod bootstrap grant for:

- node ID `subnet-screener-1`;
- provider `hetzner`;
- provider resource ID equal to the Robot server ID.

Bind the grant to the immutable, digest-pinned submission-builder image from
the private inventory. The operation fails closed if the controller epoch has
changed, its lease has expired, the node is already enrolled, or an unexpired
grant already exists. It returns the only plaintext copy of the short-lived
registration token; store it immediately in the encrypted variables file.

The source-review process does require OpenRouter. It reuses the existing
`validator-openrouter-key` Secret Manager secret, but the host does not receive
that value through GitHub, Terraform, or Ansible. Terraform creates a dedicated
`subnet-screener-1` service account with no project roles, gives it accessor on
only that one secret, and allows only this X.509 subject to impersonate it:

```text
spiffe://dittobench.ai/screener/subnet-screener-1
```

Create an offline CA on an encrypted operator device. Keep `ca.key` out of Git,
GitHub, Terraform, Ansible, and the server; only its public certificate becomes
Terraform input:

```bash
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out ca.key
openssl req -x509 -new -sha256 -days 3650 \
  -key ca.key -out ca.crt -subj '/CN=Ditto screener fleet offline CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'extendedKeyUsage=clientAuth'
```

Put the public `ca.crt` PEM in the protected Actions variable
`SCREENER_FLEET_X509_CA_CERTIFICATE_PEM`. Run the protected infrastructure plan
with `root=gcp-platform` and
`screener_fleet_x509_identity_enabled=true`, review it, then apply that exact
sealed plan. Record these non-secret outputs in the ignored inventory:

- `screener_fleet_x509_provider`;
- `screener_fleet_x509_service_account_email`; and
- `screener_fleet_x509_subject` (it must equal the literal above).

Before enabling runtime, set `screener_fleet_runtime_enabled: false` and
`screener_fleet_x509_enabled: true`, then converge once. Ansible creates the
client private key on the Hetzner host as the isolated
`ditto-screener-secrets` user and emits only a CSR. Fetch the public CSR:

```bash
ansible -b -i infra/ansible/inventory/hetzner-screener.yml \
  subnet-screener-1 -m fetch \
  -a 'src=/etc/ditto-screener-fleet/google-identity/client.csr dest=./subnet-screener-1.csr flat=true'
```

On the offline CA device, sign a short-lived client certificate. The extension
file is public; the CA private key remains offline:

```ini
[client]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectAltName=URI:spiffe://dittobench.ai/screener/subnet-screener-1
```

```bash
openssl x509 -req -sha256 -days 90 \
  -in subnet-screener-1.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -extfile subnet-screener-1-client.cnf -extensions client \
  -out subnet-screener-1.crt
```

Create an encrypted variables file outside git containing the returned
single-use enrollment grant and the two public certificates. It deliberately
contains neither the OpenRouter value nor either private key:

```yaml
screener_fleet_registration_token: replace-with-single-use-grant
screener_fleet_x509_ca_certificate_pem: |
  -----BEGIN CERTIFICATE-----
  ...
  -----END CERTIFICATE-----
screener_fleet_x509_certificate_pem: |
  -----BEGIN CERTIFICATE-----
  ...
  -----END CERTIFICATE-----
```

Encrypt it with `ansible-vault encrypt`. The grant is placed in a mode-0600
file, consumed once, and deleted after enrollment. Set
`screener_fleet_runtime_enabled: true` only after the signed certificate is
available. Ansible verifies its CA chain, client purpose, exact URI SAN,
remaining lifetime, and match to the host-generated private key before it asks
Google for a 15-minute token. A dedicated hourly service atomically refreshes
the review-only key; neither that key nor Google credentials enter a KVM build
or smoke guest.

## 3. Converge from the default Debian install

Install the pinned Ansible collections, check connectivity, then converge:

```bash
ansible-galaxy collection install -r infra/ansible/requirements.yml
ansible -i infra/ansible/inventory/hetzner-screener.yml all -m ping
ansible-playbook \
  -i infra/ansible/inventory/hetzner-screener.yml \
  infra/ansible/playbooks/hetzner-screener-fleet.yml \
  --ask-vault-pass \
  --extra-vars @/absolute/path/to/subnet-screener-1.vault.yml
```

The play installs KVM/libvirt and Docker, builds a verified disposable guest
base, validates X.509 federation without printing the secret, enrolls one node
identity, starts the trusted lane agent, and starts eight full screener
processes. The bootstrap operator receives a validated passwordless sudo rule
for future Ansible converges; root SSH is disabled. Re-run with `ansible_user`
set to that operator after the first converge.

The leaf certificate expires after 90 days. Rotate it before 14 days remain by
signing the existing public CSR again and replacing only
`screener_fleet_x509_certificate_pem` in the encrypted variables file. Ansible
keeps the host private key in place. Revocation is immediate at the Google
boundary by disabling the provider or removing the exact impersonation binding;
the service account still has no authority beyond the one secret.

## 4. Shadow in production

New nodes have zero capacity. Keep the existing provider routes unchanged and
leave every `subnet-screener-1` channel at zero while verifying its heartbeat,
Robot resource identity, exact code/image revisions, host metrics, KVM guest
creation, and local build/smoke probes. This is shadow mode: the full service is
running, but Platform cannot grant it a production lease.

Prove a cold Rust build, successful runtime smoke, failed-build/no-review, and
failed-smoke/no-review locally before changing routing. Shadow findings do not
authorize a raw database update; Backroom remains the audited policy authority.

## 5. Enforce in production

Start with one canary lane by appending this node setting through Backroom:

```text
SCREENING=1 SANDBOX=1 BUILD=1 RUNTIME=1 SOURCE_REVIEW=1
```

Set every lane to `hetzner > gcp`, enable GCE overflow with primary node
`subnet-screener-1`, backlog multiplier `3`, minimum backlog `12`, and maximum
GCE instances `6`. After one successful build -> smoke -> source-review
sequence and one terminal build failure that consumed no review lease, raise
the node to its 64 GB steady-state limits:

```text
SCREENING=8 SANDBOX=4 BUILD=4 RUNTIME=4 SOURCE_REVIEW=4
```

Backroom shows the exact confirmation phrase before it can append each
revision. Eight worker processes keep independent submissions moving, while
the four build/smoke slots cap memory pressure. For any one submission, build
and smoke finish before source review begins.

Leave the GCE MIG at zero during normal load. Before declaring the rollout
complete, verify four simultaneous cold Rust builds, four smoke transitions,
source review only after successful smoke, and one controlled primary-heartbeat
outage that scales GCE out and back to zero without moving an already-failed
job.

## Drain and update

Ordinary code updates are host-pulled and need no inbound deployment path. The
release workflow publishes an immutable fleet descriptor into the public
`ditto-subnet-stack` package, signs its exact digest with keyless Cosign, and
advances the disjoint `screener-fleet-stable-1` discovery tag only after the
descriptor has been extracted and checked. The host timer then:

1. resolves the mutable tag to an immutable digest;
2. verifies the exact `release.yml@refs/heads/main` signer and GitHub OIDC
   issuer;
3. validates the closed manifest and fleet-specific image labels;
4. fetches the signed revision from canonical public `main` and prepares both
   locked Python environments without disturbing the running release;
5. asks every service to stop claiming and drain active work, atomically moves
   `current`, and starts the new release; and
6. restores the previous link and builder digest if any service fails to start.

The host stores no GitHub token or CI SSH private key. Inspect the last accepted
descriptor and timer state without printing a secret:

```bash
sudo systemctl status ditto-screener-fleet-auto-update.timer
sudo systemctl status ditto-screener-fleet-auto-update.service
sudo sed -n 's/^\(DESCRIPTOR\|REVISION\|VERSION\|UPDATED_AT\)=/\1=/p' \
  /var/lib/ditto-screener-fleet/updater/managed-release.env
```

For host, kernel, Ansible, or emergency maintenance, first set the node to
`draining` in Backroom. This stops new full screens and lane claims while active
leases finish. Re-run Ansible, confirm the heartbeat and channel usage return
healthy, then set the node back to `active`.
