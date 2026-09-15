# Native Coding worker credentials

The default-off `coding_hosted_worker_credentials` role materializes the three
worker-owned credential files the native hosted-v2 runtime reads, on the
dedicated host `ditto-coding-hosted-v2`, without ever holding Secret Manager or
IAM access itself. The operator (Peyton) retrieves each secret and exports it in
the shell that runs the playbook; the role validates ownership, mode and
single-link state, writes atomically, verifies, and starts nothing. It is
disabled by default and, while disabled, only reports that it is dormant.

This role does not write `postgres-environment.json` (owned by the
`coding_hosted_postgres_environment` role, PR #1888) or any release or authority
file under `authority/`, `release/` or the probe receipt (handled by the
attempt-config materializer, PR #1909). It writes exactly three files.

## What it writes

All three live in `/var/lib/ditto-coding-hosted/private/`, which must already
exist as `ditto-coding-hosted:ditto-coding-hosted 0700`. The role validates that
directory and the worker home above it; it never creates, repairs or loosens
them. Each file is written `ditto-coding-hosted:ditto-coding-hosted 0600`,
regular, single link, and is re-verified after the write.

| Destination file | Format the runtime loader accepts |
| --- | --- |
| `hippius-environment.json` | JSON object with only the `DITTO_CODING_HIPPIUS_*` keys the loader's `HIPPIUS_KEYS` allows |
| `image-storage.json` | JSON object `{endpoint_url, bucket, access_key, secret_key, region}`; HTTPS is required |
| `provider-key` | Raw nonempty printable-ASCII key, no whitespace, no trailing newline |

### Fixed, non-secret settings (role constants, never inputs)

- Hippius: `DITTO_CODING_HIPPIUS_ENDPOINT_URL=https://s3.hippius.com`,
  `DITTO_CODING_HIPPIUS_REGION=decentralized`,
  `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET=ditto-subnet-coding-private-input`,
  `DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET=ditto-subnet-coding-sealed-evidence`,
  `DITTO_CODING_HIPPIUS_TIMEOUT_SECONDS=20`. These are byte-identical to the
  `coding-hippius-probe` workflow, so the storage authorities the runtime
  derives from this file equal the probe receipt's, and PR #1909 accepts them.
- Image storage: `endpoint_url=https://storage.googleapis.com`,
  `bucket=ditto-platform-agents-prod`, `region=auto`.

### Controller-side secret inputs (environment only)

Every secret enters only through a `DITTO_CODING_WORKER_*` environment variable
the operator exports on the controller. None is an Ansible variable, so
inventory, Git, vars files and `-e` never carry one; the role refuses any
`coding_hosted_worker_credentials_*` variable other than the three inputs, and
refuses any Ansible variable whose name matches a `DITTO_CODING_WORKER_*` or
`DITTO_CODING_HIPPIUS_*` controller input (so `-e DITTO_CODING_WORKER_PROVIDER_KEY=…`
is refused, not silently ignored).

| Controller environment variable | Written into | Runtime key |
| --- | --- | --- |
| `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY` |
| `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY` |
| `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY` (access key **id only**) |
| `DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY` |
| `DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY` | `hippius-environment.json` | `DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY` |
| `DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY` | `image-storage.json` | `access_key` |
| `DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY` | `image-storage.json` | `secret_key` |
| `DITTO_CODING_WORKER_PROVIDER_KEY` | `provider-key` | the raw key |

The Hippius **curator secret key** is never accepted and never written: the
runtime needs only the curator access key id to verify publications, and the
curator secret key stays with the offline curator workflow. The role refuses to
run if `DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY` or
`DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY` is set in the
environment.

Each input must be nonempty printable ASCII with no whitespace and no trailing
newline. The three access keys must start with `hip_` and be at most 512 bytes;
the two Hippius secret keys and the image secret key at most 4096 bytes; the
image access key at most 256 bytes; the provider key at most 4096 bytes. The
eight values must be distinct.

## Dedicated credentials and staging access (Peyton's decisions)

Peyton creates these identities, keys and IAM bindings himself. Nothing in this
repository, its CI or its roles creates, grants or reads them.

- **Image storage: a dedicated image-reader identity and HMAC key.** A service
  account `coding-hosted-image-reader` with a custom role holding only
  `storage.objects.get`, conditioned to screened-image objects in
  `ditto-platform-agents-prod`, and its own HMAC key. The secret key lives in
  its own `coding-hosted-image-reader-hmac-secret` (the access id in
  `coding-hosted-image-reader-hmac-access`, or taken from the identity's
  non-secret HMAC metadata). **Never reuse the Platform's
  `platform-storage-hmac-secret`**, which grants read and write on the whole
  agents bucket.
- **Provider: a dedicated, hard-capped OpenRouter key.** A new key with a hard
  credit limit sized to the canary policy, in its own `coding-hosted-openrouter-key`
  secret. **Never reuse `validator-openrouter-key`**, which the Platform relays
  use. The provider-evidence review must cover this key's account posture and
  cap.
- **Hippius identities** are reused as-is so the host's reader and evidence
  authorities equal the probe receipt's; dedicated host sub-tokens are a later
  rotation (`hippius-token-lifecycle`). The curator secret key is never staged.
- **Staging access: a two-hour, per-secret IAM condition.** For his staging
  session Peyton binds `roles/secretmanager.secretAccessor` to his own user
  principal on each secret he reads, one binding per secret (never
  project-wide, never `secretmanager.versions.add` or an admin role), each with
  the IAM condition `request.time < timestamp("<session start + 2h, UTC>")`. The
  secrets are the five Hippius secrets above plus
  `coding-hosted-image-reader-hmac-secret` and `coding-hosted-openrouter-key`.
  The binding simply stops granting at the deadline; **renew explicitly** if the
  session needs longer, by adding a fresh binding with a new two-hour deadline
  after checking what is still left to do, never by widening or removing the
  condition. Remove expired bindings afterwards so the policy stays readable.
  For example, for each secret:

  ```bash
  gcloud secrets add-iam-policy-binding SECRET --project=ditto-app-dev \
    --member="user:PEYTON" --role=roles/secretmanager.secretAccessor \
    --condition='expression=request.time < timestamp("YYYY-MM-DDTHH:MM:SSZ"),title=coding-worker-staging-2h'
  ```

## Operator procedure (Peyton runs this)

The operator retrieves each secret himself and exports it without echoing it, in
the same shell that runs the guarded entry point, then unsets everything
afterwards. Secret Manager and IAM are entirely outside the role; see
`coding-worker-credential-staging-peyton.md` for the source secret names.

Run from a fresh, clean checkout (or worktree) of the reviewed revision, with
no `ANSIBLE_*` variable exported. The guard sets `ANSIBLE_CONFIG` to the repo
`ansible.cfg` itself; do not export it.

```bash
export DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY="$(gcloud secrets versions access latest --secret=platform-coding-catalog-access-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY="$(gcloud secrets versions access latest --secret=platform-coding-catalog-secret-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY="$(gcloud secrets versions access latest --secret=platform-coding-catalog-curator-access-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY="$(gcloud secrets versions access latest --secret=platform-coding-hippius-evidence-access-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY="$(gcloud secrets versions access latest --secret=platform-coding-hippius-evidence-secret-key --project=ditto-app-dev)"
export DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY="$(gcloud secrets versions access latest --secret=coding-hosted-image-reader-hmac-access --project=ditto-app-dev)"
export DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY="$(gcloud secrets versions access latest --secret=coding-hosted-image-reader-hmac-secret --project=ditto-app-dev)"
export DITTO_CODING_WORKER_PROVIDER_KEY="$(gcloud secrets versions access latest --secret=coding-hosted-openrouter-key --project=ditto-app-dev)"

GCP_OSLOGIN_USER=… uv run --locked --script infra/scripts/coding-hosted-guarded-run.py \
  worker-credentials-materialize <reviewed 40-hex revision>

unset DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY \
  DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY \
  DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY \
  DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY \
  DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY \
  DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY \
  DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY \
  DITTO_CODING_WORKER_PROVIDER_KEY
```

Then, once, run the [first-run log spot-check](#first-run-log-spot-check) with
nothing exported.

The guard builds the enabled flag (a JSON boolean), the exact confirmation
`MATERIALIZE NATIVE CODING WORKER CREDENTIALS` and the revision itself, and
fixes `-i inventory/gcp.yml` and `--limit ditto-coding-hosted-v2`.

### Guarded entry point

`infra/scripts/coding-hosted-guarded-run.py` is the only supported way to run
the materialization, cleanup and spot-check playbooks. **Direct `ansible-playbook`
invocation is unsupported.** The script is shared byte for byte with the
PostgreSQL environment roles (#1920, #1897); each operation is a data file under
`infra/ansible/guarded-runs/` (`worker-credentials-materialize.json`,
`worker-credentials-remove.json`, `worker-credentials-journal-check.json`), so
adding an operation never edits the script or a shared registry. It:

- **Accepts only two arguments**, the operation and the reviewed 40-hex
  revision. Any other argument, including `-e`, `--start-at-task`, `-v`,
  `--step`, `--check`, `--tags`, `-i` or `--limit`, is refused. This also
  removes the `--step` residual from the supported path.
- **Verifies the checkout.** `HEAD` must equal the revision and no tracked file
  may differ. Every file under `infra/ansible` and `infra/scripts` is hashed as a
  git blob without filters and compared with the reviewed tree (so an
  `assume-unchanged` or `skip-worktree` edit, a symlink or mode swap, or a
  same-size edit is caught), and any untracked or ignored file there, such as a
  `host_vars` file, a `__pycache__` directory or a forged spec, is refused. git
  runs with no `GIT_*` variable and no system or global config.
- **Refuses dangerous environment** rather than silently stripping it: every
  `ANSIBLE_*` and `_ANSIBLE_*` variable (including `ANSIBLE_CONFIG`,
  `ANSIBLE_KEEP_REMOTE_FILES`, `ANSIBLE_DEBUG`, `ANSIBLE_VERBOSITY`,
  `ANSIBLE_LOG_PATH`, callback, strategy, plugin and library paths and
  `ANSIBLE_REMOTE_TEMP`), `LD_*`, `DYLD_*`, `OPENSSL_*`, `GCONV_*`, `GLIBC_*`, `UV_PYTHON` and its install mirrors,
  `CLOUDSDK_PYTHON*`, `SSL_CERT_FILE`/`SSL_CERT_DIR`, `UV_NO_VERIFY_HASHES`,
  `UV_INSECURE_HOST`, `UV_CONFIG_FILE`, every `PYTHON*` variable except
  `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PYTHONIOENCODING`,
  `PYTHONUTF8` and `PYTHONHASHSEED`, a preset marker, an empty or relative
  `PATH` entry, a relative `HOME`, a malformed `GCP_OSLOGIN_USER`, template
  syntax in any passed-through value, the curator secret key under either name,
  and, for cleanup and the spot-check, any exported `DITTO_CODING_WORKER_*`.
- **Validates every protected input before Ansible parses anything.** Each of
  the eight `DITTO_CODING_WORKER_*` values must be present, printable ASCII with
  no whitespace, longer than its `hip_` prefix where one applies, within the
  role's byte bound, and distinct from the other seven; and none may contain
  template syntax: `{{`, `{%`, `{#` anywhere, or `#jinja2` in any case anywhere
  (a `#jinja2:` header can redefine the delimiters and line prefixes for the
  rest of a string; ansible-core 2.21 has no other setting that changes them).
  A refusal names only the variable and a failure class (`missing`, `empty`,
  `template_syntax`, `not_printable_ascii_without_whitespace`, `missing_prefix`,
  `too_short`, `too_long`, `duplicate_value`, …), never a value.
- **Passes no secret through extra vars, argv or stdin.** The only extra vars
  are the constructed JSON boolean, confirmation and revision. The credentials
  reach Ansible only through its environment, where the role reads them.
- **Runs a fixed argv:** the locked interpreter with `-I -m ansible.cli.playbook`,
  the fixed inventory, `--limit ditto-coding-hosted-v2`, the constructed `-e` and
  the fixed playbook, from `infra/ansible`, with stdin closed. It never resolves
  `ansible-playbook` from `PATH`. `uv run --locked --script` installs
  `coding-hosted-guarded-run.py.lock`, and the script refuses unless its
  interpreter is a virtual environment holding exactly the locked distributions
  and versions, with ansible-core 2.21.2 imported from inside it. The script's
  directory is dropped from `sys.path` before any other import.
- **Builds Ansible's environment from an allowlist** (`HOME`, `USER`, `LOGNAME`,
  `PATH`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `NO_COLOR`, `SSH_AUTH_SOCK`,
  `GOOGLE_APPLICATION_CREDENTIALS`, `GCP_OSLOGIN_USER`, `CLOUDSDK_CONFIG`,
  `CLOUDSDK_ACTIVE_CONFIG_NAME`, `CLOUDSDK_CORE_ACCOUNT`, `CLOUDSDK_CORE_PROJECT`
  and the operation's
  validated inputs) plus fixed settings: `ANSIBLE_CONFIG` and
  `ANSIBLE_ROLES_PATH` in the verified tree, `ANSIBLE_KEEP_REMOTE_FILES=False`,
  `ANSIBLE_DEBUG=False`, `ANSIBLE_VERBOSITY=0`,
  `ANSIBLE_DISPLAY_ARGS_TO_STDOUT=False`, and every plugin, module and
  module_utils path pointed at a directory that cannot exist in a verified tree,
  so nothing in `~/.ansible/plugins` shadows a builtin. Pipelining stays with the
  playbook and `ansible.cfg`, as the role's guard expects.
- **Fails a run that reaches no host.** `uv run --locked --script` also
  installs the pinned `google-auth` and `requests` the `google.cloud.gcp_compute`
  inventory plugin needs, and the script refuses unless both import. Ansible
  gets `ANSIBLE_INVENTORY_UNPARSED_FAILED`, `ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED`
  and `ANSIBLE_HOST_PATTERN_MISMATCH=error`, so an inventory that does not parse
  or a `--limit` that matches no host fails the run instead of skipping every
  play and exiting 0. Each run uses a fresh `ANSIBLE_SSH_CONTROL_PATH_DIR`, so no
  ssh master connection from an earlier session is reused.
- **Refuses hidden paths.** Any untracked directory (even empty, such as a
  `playbooks/roles` that would shadow a reviewed role) and any directory it
  cannot list (an execute-only directory hides files from a walk but not from
  Ansible) is refused. The code-loading variables above are refused right after
  `sys` and `os` load, before `hashlib` or any other import can read them.
  Printed names and paths are escaped, so they cannot carry terminal escapes.
- **Sets the marker** `DITTO_CODING_HOSTED_GUARDED_RUN=<operation>`, which each
  role requires as the second task of its dynamic include.

The marker is an **accident guard only**: anyone who can run `ansible-playbook`
can export it, so it proves nothing about the caller. Every role guard below
still applies in full.

Residual trust, documented rather than closed: the operator's `PATH` (which
resolves `git`, `ssh` and the IAP ProxyCommand's `gcloud`), `~/.ssh/config`, and
the collections in `~/.ansible/collections` (the `google.cloud` inventory plugin
and the `ansible.posix` callbacks the repo `ansible.cfg` enables) run with the
credentials in the controller environment (Ansible passes its whole
environment to ssh, so the IAP ProxyCommand's `gcloud` inherits it too), so install collections only from
`infra/ansible/requirements.yml`. The script verifies the checkout it runs from,
including itself, so a modified script can skip its own checks: run it
unmodified from a fresh checkout. Code injected into the interpreter (for
example with `uv run --with`) runs before the distribution check refuses it. The
operator can still edit files between verification and use, or paste a value
elsewhere in their own shell. Tests that import a role module can leave
`__pycache__` under `infra/ansible`; the guard refuses such a checkout, so run
tests elsewhere or remove it after reviewing `git clean -ndX infra`.

## What the role refuses

- The enabled gate is frozen once in `main.yml` with `is sameas true` (never
  `| bool`, which prints the coerced value in a deprecation warning even under
  `no_log`), in a task with no loop variable in scope, and the enabled branch
  runs through a dynamic `include_tasks`. ansible re-templates a variable on
  every read, so a lazily templated extra var such as
  `-e '{"..._enabled": "{{ item is defined }}"}'` evaluates false at the gate and
  true inside a write loop; a block-level `when` is pushed down to every child
  task. Freezing the gate once and using a dynamic include closes that flip.
- The preset refusal is enforced twice: once in `main.yml` before the gate is
  frozen, and again as the first task inside the dynamically included
  `materialize.yml` / `remove.yml`. `--start-at-task` can begin at the `main.yml`
  gate freeze and skip the static refusal, but it cannot jump into a dynamic
  include, so the in-include refusal always runs. It refuses any
  `coding_hosted_worker_credentials_*` variable other than the three inputs and
  the gate (a preset registered result, a preset capture such as
  `coding_hosted_worker_credentials_documents`, or an undocumented input), any
  `DITTO_CODING_WORKER_*` / `DITTO_CODING_HIPPIUS_*` Ansible variable, and it
  asserts the raw `coding_hosted_worker_credentials_enabled is sameas true`, so a
  preset gate fact alone (with the flag false or unset) cannot open the run. The
  refusal pattern excludes the cleanup role's prefix, and the cleanup refusal
  excludes this one's, so neither matches the other's variables.
- The run must target exactly the one dedicated host. `hosts: role_coding_hosted`
  makes an `inventory_hostname`/group check tautological, and a host reports its
  own name, so the role asserts both `ansible_play_batch == ['ditto-coding-hosted-v2']`
  and `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` — values computed
  from the run's targeting that `-e` cannot override. Requiring the whole host
  set, not only the batch, means `serial: 1` on the group cannot pass either, so
  a role-labelled VM that merely reports this hostname is never reached without
  `--limit ditto-coding-hosted-v2`.
- The write module carries the credentials to the host, so before any
  secret-carrying task the role refuses unless SSH pipelining is on and
  `ANSIBLE_KEEP_REMOTE_FILES` is unset, so the module is never left in the host's
  remote temp directory. The materialize playbook sets `ansible_pipelining: true`
  in its play vars: the SSH connection plugin reads pipelining from that var (a
  `[ssh_connection] pipelining` ini setting in `ansible.cfg` turns pipelining on
  but does **not** populate the var), so setting it there both turns pipelining
  on and lets the guard confirm it. The guard requires `ansible_pipelining` true
  and `ansible_ssh_pipelining` undefined or true, because on ansible-core 2.21.2
  `ansible_ssh_pipelining` wins over `ansible_pipelining`, so
  `-e ansible_ssh_pipelining=false` (or `-e ansible_pipelining=false`) turns
  pipelining off and is refused. This is advisory against accidental
  misconfiguration.
- Every accepted input is captured once with `set_fact` and validated as a
  frozen literal. A `set_fact` result is a plain value, not a trusted template,
  so it never re-templates in a later scope. The confirmation and source
  revision are matched exactly, and a value that a nested template rendered into
  a literal `{{`, `{%` or `{#` is refused.
- The playbook gathers no facts. Host identity and the worker and custodian
  accounts come from a registered `setup` and `getent`, because an
  `ansible_facts` extra var replaces gathered facts.
- The worker and every custody instance must be stopped. The live-unit guard is
  an allow-list read directly from the registered `systemctl` result in an
  inlined assert (no overridable include variable), by both the pre-write and
  post-write checks: only `inactive` or `failed` pass, so `active`,
  `activating`, `deactivating`, `reloading`, `refreshing` (systemd 256 and
  later), `maintenance`, a future state or an unparseable line all refuse.
  An empty listing means no such unit is loaded and is allowed. The role stops
  nothing.
- No process may be running as the worker UID. The listed units are not enough:
  the rootless dockerd user manager (`user@<uid>.service`) and an escaped
  candidate share that UID and could read the files or swap a directory. The
  role reads `/proc` for the worker UID and refuses if any process is present.
- The private directory must be the worker's own `0700` directory, not a
  symlink, below a real worker home that is not group- or world-writable. This
  Ansible stat is an early, clear refusal; the authoritative symlink-safe check
  is on the module's own file descriptors (below).

## How it writes and verifies

The write is done by a role-local Ansible module,
`library/coding_hosted_worker_credentials_write.py`, that ansible transfers to
the target and runs as root. The three documents are one module parameter
declared `no_log: true` in its `argument_spec`, so ansible replaces it with
`VALUE_SPECIFIED_IN_NO_LOG_PARAMETER` in the target's module-invocation journal
line and in `-vvv` controller output; nothing is passed in argv or on stdin, and
the module task is `no_log` too. The module never resolves the destination as a
string: it opens every component of the private directory path from `/` with
`O_NOFOLLOW|O_DIRECTORY` — so a directory the worker account could swap for a
symlink between the Ansible stat and the write cannot redirect it — verifies the
home and private directory's owner and mode on the open descriptors, writes each
file to a tracked temporary with `O_CREAT|O_EXCL|O_NOFOLLOW`, `fchown`s and
`fchmod`s it, `fsync`s, renames every temporary into place only after all three
are written, and re-verifies each result (regular, single link, owner, mode
`0600`, size, and SHA-256 of the bytes, compared in process) on its own
descriptor. Every temporary it creates is unlinked on any failure path, so a
failed write leaves no partial secret temporary behind. It refuses a destination
that is a symlink, directory, hard link, another account's file or not mode
`0600`, and never follows or re-permissions such a path. The module prints only
non-secret metadata — filenames, mode, link count, size, owner — never a value
or a digest. After it, the role re-lists the units and refuses loudly if any
unit went live during materialization.

Cleanup uses the sibling module `coding_hosted_worker_credentials_unlink.py`,
which opens the path the same way and removes the three fixed names, and any
leftover `.<name>.*.tmp` a partial write may have left, with `unlinkat` on the
pinned directory descriptor, reporting which it removed.

Partial write: the module writes all three temporaries before renaming any, so a
rename failing part-way is rare, but if it happens the module fails and reports
exactly which fixed names were `replaced` and which were left `unchanged_or_unknown`;
it does not silently leave a mixed set unreported. Reconcile by hand from that
report before re-running. A re-run overwrites any already-replaced file with the
same content, so re-running after reconciling is safe.

Residual race: a unit that starts after the final recheck and before any later
service start is outside this role, which starts nothing. Start services only
after re-confirming the files and the stopped state through the reviewed
procedure.

## Nothing is logged, and the direct-invocation residual

No task prints an input value or a digest. The set_fact captures that hold
credentials are `no_log`; the write module carries them as a `no_log`
`argument_spec` parameter and its task is `no_log`; asserts are `quiet` with
static failure messages that never interpolate an input; the modules and report
show only filenames, states and the non-secret source revision. The rehearsal
(below) proves that stand-in secrets and their MD5, SHA-1, SHA-256 and SHA-512
digests, in raw, JSON-, YAML- and repr-escaped forms, never reach ansible
output, including under `-vvv` and `--diff` with the repo's yaml callback.

Inputs are captured once with `set_fact ... | default('', true)` and validated
as frozen literals, and the enabled gate is frozen the same way. On ansible-core
2.21.2 this absorbs an **undefined-class** template error — for example
`{{ {}[lookup('env','X')] }}`, whose subscript raises an Undefined — into `''` or
`false`, so such an input is refused with no leak (the rehearsal exercises this).

It cannot absorb a template that raises a **lookup or filter plugin error whose
message embeds a value**, for example
`{{ lookup('file', lookup('env','DITTO_CODING_WORKER_PROVIDER_KEY')) }}` as an
input: ansible prints that value in its own `[ERROR]` finalization banner before
the role can inspect it, and neither `no_log`, `ignore_errors`, a rescue nor
`default(..., true)` suppresses that banner (all were tested). The run still
writes nothing. **The guarded entry point closes this on the supported path**:
it accepts no `-e`, builds every extra var itself, refuses template syntax in
every credential and passed-through value before Ansible starts, and refuses
`ANSIBLE_LOG_PATH` and every other `ANSIBLE_*` override. The residual remains
only for an unsupported direct `ansible-playbook` run, where the hostile
template would be typed by the operator who already holds the exported values.

A second residual is also limited to direct runs: `ansible-playbook --step`
prompts before each task and lets the operator skip a guard. The guard refuses
`--step` and every other argument.

## Cleanup and rotation

`coding_hosted_worker_credentials_cleanup` (playbook
`gcp-coding-hosted-worker-credentials-cleanup.yml`, confirmation
`REMOVE NATIVE CODING WORKER CREDENTIALS`) removes only the three fixed files
through the `coding_hosted_worker_credentials_unlink` module. It refuses unless
the units are stopped and no process runs as the worker UID, opens every path
component with `O_NOFOLLOW`, and removes each name with `unlinkat` — never
`file: state=absent` on a path that could be a directory or symlink. The module
returns `removed`, `already_absent`, `refused` and `not_attempted` lists on every
path, so a partial removal (for example a later name that is a symlink after
earlier names were unlinked) names exactly what was removed, what refused and
why, and what was not attempted. It also removes and reports any leftover
`.<name>.*.tmp` a partial write may have left. It reads no secret, so export
nothing for it. It keeps the directories.

Asymmetry with the write module: the write module requires the private directory
to be mode `0700` and each destination mode `0600`, but cleanup only requires the
directory to be owned by the worker and not group/other writable, so a directory
left at a wrong mode can still be cleaned up; the observed directory mode is
reported (`private_dir_mode`) for the operator to reconcile. Removing a file does
not depend on the file's own mode.

### Removal is not revocation

Removing these files does not revoke any credential. Each stays valid at its
issuer until the operator revokes or rotates it there: the Hippius reader and
evidence tokens (`hippius-token-lifecycle`), the GCS HMAC key for image storage,
and the OpenRouter provider key. A suspected exposure is a leak of those
credentials; rotate them at the issuer, not by running cleanup. Runtime roots
written by earlier attempts may also hold copies and are retained evidence this
role neither finds nor removes.

## Non-goals

- No Secret Manager access and no IAM change. The role reads no secret from a
  cloud API; the operator supplies every value through the controller
  environment.
- No service is installed, started, restarted or enabled. There is no `systemd`,
  `service`, handler or command that starts anything; only read-only state
  queries.
- No release, authority, probe-receipt or PostgreSQL file is touched.

## First-run log spot-check

After the **first** materialization, and before starting anything, Peyton runs
the default-off redacted spot-check once, with nothing exported:

```bash
GCP_OSLOGIN_USER=… uv run --locked --script infra/scripts/coding-hosted-guarded-run.py \
  worker-credentials-journal-check <reviewed 40-hex revision>
```

The guard refuses it while any `DITTO_CODING_WORKER_*` variable is exported and
builds the confirmation `CHECK NATIVE CODING WORKER CREDENTIAL JOURNAL` itself.
The `coding_hosted_worker_credentials_journal_check` role has the same frozen
gate, in-include preset refusal, marker, single-host pin, pipelining guard,
check-mode refusal, exact confirmation and revision, and registered host
identity as the materialization role. It writes nothing, starts nothing and
needs no controller secret.

Its role-local module, in a `no_log` task with only literal, non-secret
arguments, opens the three credential files through pinned descriptors
(`O_NOFOLLOW` from `/`, regular single-link worker-owned files, a symlink,
absent or foreign file refused), derives needles in memory, and scans:

- **the whole journal** as `/usr/bin/journalctl --no-pager --quiet
  --output=export`, which carries every field of every entry (`MESSAGE`,
  `_CMDLINE`, `SYSLOG_IDENTIFIER` and the rest), not only messages;
- **text logs** in `/var/log` whose names start with `syslog`, `messages`,
  `auth.log`, `daemon.log`, `user.log`, `kern.log`, `debug` or `sudo`, including
  gzip-rotated ones;
- **Ansible temp locations**: every entry in `/root/.ansible/tmp` and
  `/home/*/.ansible/tmp`, and `ansible*`/`AnsiballZ_*` entries in `/tmp` and
  `/var/tmp`, counted as leftovers and their files scanned.

It counts, per source and per class:

| Class | Needles |
| --- | --- |
| `value_raw` | each of the eight credential values |
| `value_encoded` | each value JSON-escaped, doubly JSON-escaped, backslash-doubled, repr-escaped, URL-encoded (both forms), lower- and upper-case hex, and base64 at every byte alignment, standard and URL-safe |
| `value_digest` | MD5, SHA-1, SHA-256 and SHA-512 hex of every value and every whole file |
| `env_name` | the eight `DITTO_CODING_WORKER_*` input names and the curator secret key's |
| `runtime_key_name` | the secret-bearing `DITTO_CODING_HIPPIUS_*` key names |
| `file_name` | `hippius-environment.json`, `image-storage.json`, `private/provider-key` |
| `input_name` | the role's input and capture variable names and the materialization confirmation |
| `module_temp_file` | `AnsiballZ_coding_hosted_worker_credentials_write`, which appears only if the write module was ever written to disk |

A value shorter than 8 bytes, or an encoded form shorter than 12, is not
searched, to avoid matching unrelated bytes; a short value is counted in
`values_below_minimum_length` and makes the run not clean.

The module returns only integers and booleans: never a matching line, value,
needle or digest, and nothing is written to a temp file, a register beyond
those counts, or the journal (the task is `no_log`). A module failure reports a
fixed state or the exception class only. The report prints `clean=true|false`,
`journal_scanned`, the per-class `totals`, the per-source counts,
`text_log_files`, `ansible_temp_entries` and `values_below_minimum_length`, and
the role then fails unless the run is clean: the journal was read, every count
is zero, no Ansible temp entry was left and no value was too short.

Interpreting a failure: a `value_*` count is an exposure of that credential;
**revoke and rotate** it at its issuer, then clean up and re-materialize. A name
class or `ansible_temp_entries` count without a value count means something
logged credential-adjacent text or left an Ansible temp entry; inspect the host
by hand (the counts say where, never what) before starting services. The check
covers only what is on the host at that moment; it is not continuous
monitoring, and the journal's own retention bounds how far back it sees.

## Tests

Structural pytest tests parse the roles and assert their shape, the `no_log`
coverage, the include gate, the guard specs and markers, and the CI wiring. A
platform-side test renders the three documents and feeds them to the real
runtime parsers. With `DITTO_ANSIBLE_REHEARSAL=1` the rehearsal runs the enabled
tasks through ansible-core 2.21.2 against a temporary tree with stand-in
credentials: it writes the files when units are stopped; refuses forged facts,
preset results and captures, undocumented and credential-shaped variables, a
templated (`lookup`) input, wrong unit states, a trailing-newline revision,
missing, duplicate and wrong-prefix credentials, wrong-owner/mode/symlink/hardlink
destinations, the `--start-at-task` and enabled-flip bypasses, a unit that goes
live after the write, and a run without the guard's marker or with another
operation's; and proves the stand-ins and their digests never appear in output.

`ditto/tests/test_coding_hosted_worker_credentials_journal_check.py` runs the
spot-check module against synthetic credential files, synthetic journal exports
(matches in `MESSAGE` and `_CMDLINE`), gzip-rotated logs and Ansible temp trees:
every class is counted, matches split across read chunks count once, a failing
journal command is never clean, linked, absent and foreign files are refused,
and the result holds only integers and booleans with no value, needle or digest.
Its rehearsal runs the role and module through ansible-core 2.21.2 under `-vvv`:
a clean host reports `clean=true`, planted base64 and name findings fail at the
final check with counts only, and a missing marker refuses before the scan.

`ditto/tests/test_coding_hosted_guarded_run.py` (shared with #1920 and #1897)
tests the guarded entry point against synthetic git checkouts and, with the
rehearsal gate, runs the real script under `uv run --locked --script` and
ansible-core 2.21.2: the play receives only the constructed values and marker,
a `PATH` `ansible-playbook` is never used, and a raising-template input, a
template value, `ANSIBLE_CONFIG`, a `--with` package and extra arguments are
refused before Ansible starts. The infra CI Ansible job runs all three
rehearsals and the three disabled fixtures.
