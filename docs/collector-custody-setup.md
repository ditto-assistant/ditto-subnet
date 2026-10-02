# Collector delegate custody: reviewed provisioning procedure

This is a default-off custody setup for the existing collector runner, not a
replacement collector or permission to activate earnings, weights or timers.
The legacy `treasury-host.tf` / `treasury_create_key.py` single wallet is not used.
Primary collector and holding keys remain offline with Peyton.

## Concrete resource plan

`infra/terraform/stacks/gcp-collector-custody/collector-custody.tf` creates nothing unless
`enable_collector_custody=true` is explicitly reviewed. The proposed resource set:

- Dedicated `sn118-collector-custody` VPC/subnet, private Google API access,
  IAP-only TCP22 ingress; private-address and all-other egress denies.
- `sn118-collector-registration-signer` and
  `sn118-collector-transfer-signer`, shielded private Debian13 VMs, no external
  IP, no project SSH keys. Separate dedicated service accounts
  `sn118-collector-registration` / `sn118-collector-transfer`.
- Distinct `sn118-collector-registration-delegate` /
  `sn118-collector-transfer-delegate` secret containers. Terraform never creates
  a secret version or receives payloads. Secret/host destruction is refused;
  boot disks survive VM deletion. No service-account keys or project-wide
  runtime permissions are created.
- Each role's bootstrap tag alone permits temporary NAT internet access. The
  RFC1918 deny outranks that allow. An armed peer never shares a bootstrap tag.
  NAT disappears when neither role is bootstrapping.
- A single unprivileged add/list custom role, bound to one matching secret only
  during its armed phase. It cannot access secret payloads, administer secrets,
  manage IAM, impersonate principals, enable/disable/destroy versions or sign
  chain transactions.
- A later sealed per-secret accessor binding with exact IAM condition
  `resource.name == projects/<numeric-project>/secrets/<role-secret>/versions/1`.
  No `latest` alias, other role secret or new version is accepted.

Proposed VMs are `e2-standard-2`, 30GB pd-balanced boot disks, exact image
`projects/debian-cloud/global/images/debian-13-trixie-v20260921`; these are
reviewable choices, not an assertion of applied resources. Inspect project/org
inherited IAM, public API access policy, OS Login administrators and effective
firewall rules as well as these new resource bindings. A project owner can read
secrets; this design does not promise secrecy from authorized cloud owners or a
compromised signer root. Native Cloud KMS is not claimed to perform sr25519.

## Protected plans and phases

Use the existing protected `Infrastructure plan or apply` workflow for the
`gcp-collector-custody` root, exact main plan SHA/run/checksum and independent review.
Do not run local apply, target individual grants, change generated plan bytes,
or enable dormant legacy treasury resources. Each phase needs a fresh full plan
and the separately reviewed public production intent; never let an omitted CLI
flag reset a live role's phase. The dedicated root's `prod.auto.tfvars` retains
the reviewed public bootstrap intent, five addresses and exact source pin.
No secret version is supplied or managed by Terraform.

The first `gcp-platform` plan (36942490042, source c904afec) was rejected:
its 23 custody creations were mixed with 30 unrelated changed resources.
Custody had no managed state or actual hosts/secrets, so its definitions and
template were moved to one dedicated owner without migrating/forgetting state.
The dedicated GCS state prefix is `gcp-collector-custody`; its provider lock
pins Google 6.50.0. The workflow does not inject Platform/Cloudflare secrets,
screener release pins, hotkey phases or dev-host variables into custody plans.
Both plan sealing and checksum-verified apply refuse unrelated resources.

Before the first custody plan, the existing `gcp-preview` root must bootstrap
two additive conditional grants for the existing Terraform plan identity:
Object Admin on only `gcp-collector-custody/default.tflock`, and Object Creator
on only `gcp-collector-custody/default.tfstate`. Existing Object Viewer remains
unchanged. The observed preview state serial 4 had sixteen managed resources
and no staged bake identity/compute/WIF, so `enable_preview_bake=false` preserves
that absence. The protected full preview plan must show only these two grant
creations with all sixteen existing resources unchanged. A scope fence rejects
other changes when bootstrap grants are added. Review the exact binary before
applying it; no direct grant, target bypass or state mutation shortcut.

Required public inputs are project, operator, five offline public addresses,
exact reviewed main source SHA and independent role phases. The root defaults
already name `ditto-app-dev`; custody must nevertheless use Peyton's expressly
selected project. SHA must include this ceremony, be reachable from advertised
main, match root-owned source and frozen `uv.lock`. Bootstrap asserts SDK10.5.0.

1. **Bootstrap** both roles with no secret permission. Verify actual image,
   identities, source/lock/venv files, root ownership, no long-lived bootstrap
   process, ready marker and no activation/timer/journal. Record image/source
   hashes and network/principal facts. OS Admin Login/IAP are per instance;
   the operator also receives Service Account User on each exact attached
   identity, required by OS Login. No token-creator or project-wide grant is added.
2. **Armed**: apply removes that role's bootstrap internet tag/allow first, then
   gives its own add/list secret binding. Verify actual effective Google-only
   egress (`199.36.153.4/30:443`), no private/internet path and no inherited secret
   payload access or cross-role grant before generating. A skipped bootstrap
   cannot install anything during an armed boot; the generator also checks the
   actual metadata host/project/service-account/tags.
3. On matching host as root, run `collector-delegate-ceremony generate` once.
   It sends a freshly generated 24-word mnemonic directly from memory over TLS
   to Secret Manager with CRC32C. It returns only role, public address, explicit
   first version and status. No mnemonic in disk wallet, Terraform state,
   metadata, shell arguments/environment, stdout/stderr, USB or operator host.
   Core dumps are disabled; this is process-memory custody, not hardware erasure.
4. **Locked**: separate protected apply removes all generation bindings. Verify
   effective add/access are both refused; preserve public ceremony intent and
   receipt. Do not combine this checkpoint with reader creation.
5. **Sealed**: separate protected apply grants own fixed version1 access only.
   Verify effective read of wrong version/other role is refused and add/list is
   refused. On each matching host run `collector-delegate-ceremony verify`.
   It rederives the address in memory and compares exact retained public receipt
   and five forbidden offline roles. Independently compare both public addresses
   as distinct and bind numerical versions to the signed collector policy.

The sealed custody network still permits only Google APIs. It is not an enabled
chain signer network/runtime; a separately reviewed chain RPC egress/runtime/
unit installation and activation plan is required. No collector unit, timer,
policy envelope, journal, on-chain proxy grant, auto-stake route, registration,
funding dispatch or treasury setting is installed or invoked by this bootstrap.

## Uncertainty and recovery

Generation fsyncs a public intent before it even calls the key generator. Any
existing intent, temporary receipt or existing secret version (enabled, disabled
or destroyed) refuses another generation. Upload is one attempt, without retry.
A timeout or crash retains public recovery evidence and never rotates silently.

If receipt has `pending-upload` and version1 exists, first remove writer
permission via locked, then seal and verify the stored key against that exact
receipt. A checksum/API mismatch is not successful storage. If no key is stored,
or public intent lacks an address, stop for audited explicit recovery; do not
delete the intent, reopen generation, destroy the secret or substitute a new
version automatically. Repeated verify is read-only. Never print an HTTP error
body, traceback or SDK exception carrying payloads.

After offline primary grants, a stolen Registration/Transfer delegate can act
outside application caps. Keep separate bounded fee balances, offline revocation
and current finalized identity/filter checks. Custody completion alone is not
chain authority or vendor-payment approval.

## Validation

- `python3 -m unittest discover -s ditto/tests -p test_collector_delegate_ceremony.py -v`
  exercises both roles, interrupted uploads, durable intents, existing-version
  refusal, metadata/principal/tag mismatch, checksums, error redaction, collisions,
  symlinks/private ownership, locking and matching-role rederivation.
- `bash scripts/test-collector-custody.sh` copies actual production resources
  into a credentials-free Terraform fixture. Mock-provider plans prove default
  absence, bootstrap, armed/mixed isolation, locked removal, sealed numerical
  version bindings and invalid-input refusal. No backend or real GCP API calls.
- Exact-head hosted workflow `Collector custody refusal controls` has only
  `contents:read`, no OIDC, credentials, production environment or apply.

Mock plans and tests do not prove live inherited IAM/firewalls or custody.
Before provisioning, independently inspect the exact protected real plan and
source-bound test results, then verify actual cloud state at each phase.
