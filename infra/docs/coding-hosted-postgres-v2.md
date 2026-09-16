# Native-v2 private PostgreSQL path

This layer prepares the private network and guest admission path from the
Platform-owned Coding host to the existing Platform PostgreSQL VM. Both network
and guest gates default off. It creates no database user, password, grant, worker,
assignment or private evaluation, and performs no protected apply or convergence.
The [host foundation](coding-hosted-host-v2.md) remains independently default-off.

## Network boundary

`enable_coding_hosted_postgres` requires `enable_coding_hosted_host`. The root
passes the actual `module.pg_vm.internal_ip`, Platform VPC self-link and existing
PostgreSQL target tag into the native-host module. The optional `postgres_peer`
input defaults null and rejects disabled hosts, another project's/network's
self-link and addresses outside the current Platform `10.30.0.0/24` IPv4 subnet.

Two reviewed VPC peerings exchange private subnet routes. Custom routes, public
subnet routes and IPv6 exchange are disabled. Peering is not a port-specific
route or a firewall grant: private subnet routes are automatically exchanged by
[Google Cloud's peering model](https://docs.cloud.google.com/vpc/docs/vpc-peering).
The only new access permissions are separate firewall rules:

- Coding egress: its existing VM tag to the actual PostgreSQL `/32`, TCP 5432,
  at priority 800 before the existing priority-900 private/metadata deny.
- PostgreSQL ingress: the actual Coding VM's single `/32` to the existing
  PostgreSQL target tag, TCP 5432. No whole-subnet or cross-VPC source-tag grant.

Peering creation depends on both rules. The original private/metadata and other
egress denials remain intact; no reverse inbound exception is added on Coding.
Review effective inherited policies and both networks' rules before apply.
VPC rules cannot distinguish trusted worker and candidate processes sharing one
VM: separately qualify the default-deny host/cgroup/router boundary before
granting this route. The route does not approve a private execution environment.

The nonsecret `coding_hosted_postgres_access` output records the exact client IP,
client `/32`, PostgreSQL private IP and port. It explicitly reports
`database_login_ready=false`, `shadow_only=true` and `weight_eligible=false`.
An IP change after host replacement requires renewed review and guest convergence;
do not reuse an old host/IP approval or destroy retained host state automatically.

## Guest firewall and PostgreSQL admission

After separately reviewing the protected Terraform plan/apply and actual host
boundary, supply `coding_hosted_postgres_enabled: true` and the exact
`coding_hosted_postgres_client_ip` from that output to the existing
`gcp-platform-pg.yml` convergence. Do not guess an address or place passwords in
inventory, Git, receipts or command output. Existing protected password handling
and OS Login/IAP access remain unchanged.

The guard runs before the `base` role can change UFW, and again for direct
`postgres` role callers. It permits only `ditto-pg-platform` in its proper
inventory group and a canonical single usable native-subnet address. Reserved,
public, other-subnet, CIDR, IPv6, wrong-type and newline/injection inputs fail.

With the guest flag off, both existing allowlists render unchanged. When enabled:

- UFW gains only TCP 5432 from the exact host `/32`.
- HBA gains only `ditto_platform_prod`, the existing `ditto` Platform application
  principal, that same `/32`, and `scram-sha-256` authentication. Existing Platform
  subnet rules remain unchanged; the new rule grants neither dev-database nor
  all-user admission from Coding.

This uses the existing trusted Platform application principal and does not change
its SQL privileges. Its credential must never reach validators, miners or
candidate containers. Approval of that principal's use by the trusted native
service, credential custody, and authenticated/encrypted database-channel policy
remain operator responsibilities. Neither a private VPC route nor SCRAM alone
is a claim of certificate-verified TLS. No public PostgreSQL endpoint, PgBouncer,
new database, key distribution or password rotation is added by this layer.

HBA-only changes now notify a PostgreSQL reload, not a restart. Tuning changes
that require restart retain their existing restart handler. A full database
convergence can still change other approved settings; review the complete diff
and any resulting maintenance needs rather than treating it as HBA-only blindly.

## Qualification and rollback

After convergence, verify the actual routes, source IP, cloud and guest firewall
rules, effective HBA rows, credential/TLS behavior and a bounded native-service
database query. Prove rejection from other hosts and candidate containers and
against unintended ports, databases and users. Then continue approved runtime
installation, unchanged-private-suite qualification, custody/signing, Hippius
release publication and the private shadow canary. None of those live proofs is
provided by mocked plan or configuration-rendering tests.

Rollback is explicit and must preserve private state:

1. Stop/drain the exact native invocation and revoke its host network authority;
   reconcile any unfinished attempt/evidence before another run.
2. Review removal of only the PostgreSQL peerings and exceptions by setting the
   separate network flag false. Keep the host flag true if retained host data
   must remain; do not decommission the host as a network rollback shortcut.
3. Remove the exact native UFW rule through an authorized targeted operation.
   UFW convergence is additive: omitting a rule does not delete an existing one.
   Never reset the complete guest firewall.
4. Set the guest flag false and converge/reload the managed HBA configuration.
   A reload does not terminate existing connections. Inspect exact Coding-host
   backend identities and obtain approval before terminating any retained
   sessions; do not kill all PostgreSQL clients.

The module tests use a mocked provider and `command=plan` only. The Ansible
fixture forces a local connection, has no convergence roles or privilege
escalation, and checks native rendering plus 13 invalid addresses. CI syntax
checks the real playbook and runs that fixture with `--check`. These tests do not
read production database credentials or apply network/host changes.

## Native environment files

The hosted worker and the custody service each read their own PostgreSQL
environment copy. The private reader requires a file owned by the reading
account, mode `0600`, single-link, inside a `0700` directory it owns, so one
shared root-owned file cannot serve both. The default-off
`coding_hosted_postgres_environment` role writes:

- `/var/lib/ditto-coding-custody/private/postgres-environment.json`, owned by
  `ditto-coding-custody`;
- `/var/lib/ditto-coding-hosted/private/postgres-environment.json`, owned by
  `ditto-coding-hosted`.

Each is the JSON list of `POSTGRES_*=value` entries that the runtime parser
accepts:
- **Fixed fields:** user `ditto`, database `ditto_platform_prod`, port `5432`,
  pool 1–4 and a 30-second command timeout.
- **Host:** the exact Platform private IP.
- **Password:** read only from `DITTO_CODING_PG_PASSWORD` in the controller's
  local environment. It is not an Ansible variable, so inventory, Git and `-e`
  cannot carry it, and a `coding_hosted_postgres_environment_password` variable
  is refused.

This is a separate protected convergence. An operator who already holds
`platform-db-password` exports it, and the exact Platform private IP from the
reviewed `coding_hosted_postgres_access` output, in the controller shell, runs
the guarded entry point, and unsets both. No workflow identity gets Secret
Manager access.

```bash
# From a fresh, clean checkout of the reviewed revision.
export DITTO_CODING_PG_PASSWORD="$(gcloud secrets versions access latest \
  --secret=platform-db-password --project=ditto-app-dev)"
export DITTO_CODING_PG_HOST=10.30.0.…     # coding_hosted_postgres_access output
GCP_OSLOGIN_USER=… uv run --locked --script infra/scripts/coding-hosted-guarded-run.py \
  postgres-environment-materialize <reviewed 40-hex revision>
unset DITTO_CODING_PG_PASSWORD DITTO_CODING_PG_HOST
```

### Guarded entry point

`infra/scripts/coding-hosted-guarded-run.py` is the only supported way to run
this playbook and the removal playbook below. Direct `ansible-playbook`
invocation is unsupported. The script is shared byte for byte with the worker
credential roles; each operation is a data file under
`infra/ansible/guarded-runs/` (here `postgres-environment-materialize.json`),
so adding an operation never edits the script or a shared registry. It:

- **Accepts only two arguments**, the operation name and the reviewed 40-hex
  revision. Any other argument, including `-e`, `--start-at-task`, `-v`,
  `--step`, `--check`, `--tags`, `-i` or `--limit`, is refused.
- **Verifies the checkout.** `HEAD` must equal the revision and `git status`
  must show no tracked change. Because `status` trusts the index stat cache,
  `assume-unchanged`/`skip-worktree` bits and clean filters, every file under
  `infra/ansible` and `infra/scripts` is also hashed as a git blob, without
  filters, and compared with the reviewed tree, including symlinks and the
  executable bit. Any untracked or ignored file there, such as a `host_vars`
  file, a `__pycache__` directory or a forged spec, is refused. git runs with
  no `GIT_*` variable and no system or global config.
- **Refuses dangerous environment** rather than silently stripping it:
  every `ANSIBLE_*` and `_ANSIBLE_*` variable (config file, keep remote files,
  debug, verbosity, log path, callbacks, strategy, plugin and library paths,
  remote temp and all others), `LD_*`, `DYLD_*`, `OPENSSL_*`, `GCONV_*`, `GLIBC_*`, `UV_PYTHON` and its install mirrors,
  `CLOUDSDK_PYTHON*`, `SSL_CERT_FILE`/`SSL_CERT_DIR`, `UV_NO_VERIFY_HASHES`,
  `UV_INSECURE_HOST`, `UV_CONFIG_FILE`, every `PYTHON*` variable except
  `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PYTHONIOENCODING`,
  `PYTHONUTF8` and `PYTHONHASHSEED`, a preset marker, an empty or relative
  `PATH` entry, a relative `HOME`, a malformed `GCP_OSLOGIN_USER`, template
  syntax in any passed-through value, and any variable the operation forbids.
- **Validates every protected input before Ansible parses anything.** Here
  `DITTO_CODING_PG_PASSWORD` must be one line of 1 to 1024 characters with no
  control character or surrounding whitespace, and `DITTO_CODING_PG_HOST` must
  fully match the role's own address pattern. Both refuse template syntax:
  `{{`, `{%`, `{#` anywhere, and `#jinja2` in any case anywhere, since a
  `#jinja2:` header can redefine the delimiters and line prefixes for the rest
  of a string. ansible-core 2.21 has no configuration that changes the
  delimiters otherwise. A refusal names only the variable and a failure class
  (`missing`, `empty`, `template_syntax`, `control_character`,
  `surrounding_whitespace`, `too_long`, `pattern_mismatch`, …), never a value.
- **Builds the extra vars itself:** the JSON boolean
  `coding_hosted_postgres_environment_enabled: true`, the exact confirmation,
  the revision, and the pattern-validated, non-secret host. No secret is passed
  through extra vars, argv or stdin; the password reaches Ansible only in its
  environment, where the role reads it.
- **Runs a fixed argv:** the locked interpreter with `-I -m ansible.cli.playbook`,
  `-i infra/ansible/inventory/gcp.yml`, `--limit ditto-coding-hosted-v2`, the
  constructed `-e` and the fixed playbook, with `infra/ansible` as the working
  directory and stdin closed. It never resolves `ansible-playbook` from `PATH`.
  `uv run --locked --script` installs `coding-hosted-guarded-run.py.lock`, and
  the script refuses unless the interpreter is a virtual environment whose
  installed distributions are exactly the locked set and versions, with
  ansible-core 2.21.2 imported from inside it. The script's own directory is
  removed from `sys.path` before any other import.
- **Constructs Ansible's environment from an allowlist:** `HOME`, `USER`,
  `LOGNAME`, `PATH`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `NO_COLOR`,
  `SSH_AUTH_SOCK`, `GOOGLE_APPLICATION_CREDENTIALS`, `GCP_OSLOGIN_USER`,
  `CLOUDSDK_CONFIG`, `CLOUDSDK_ACTIVE_CONFIG_NAME`, `CLOUDSDK_CORE_ACCOUNT` and
  `CLOUDSDK_CORE_PROJECT`,
  the operation's validated secret inputs, and fixed settings:
  `ANSIBLE_CONFIG` and `ANSIBLE_ROLES_PATH` in the verified tree,
  `ANSIBLE_KEEP_REMOTE_FILES=False`, `ANSIBLE_DEBUG=False`,
  `ANSIBLE_VERBOSITY=0`, `ANSIBLE_DISPLAY_ARGS_TO_STDOUT=False`, and every
  plugin, module and module_utils search path pointed at a directory that
  cannot exist in a verified tree, so nothing under `~/.ansible/plugins` or
  `/usr/share/ansible/plugins` shadows a builtin. Pipelining is left to the
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
- **Sets the marker** `DITTO_CODING_HOSTED_GUARDED_RUN=<operation>`, which the
  role requires as the second task of its dynamic include.

The marker is an **accident guard only**. Anyone who can run `ansible-playbook`
can export the same variable, so it proves nothing about the caller; it stops
an accidental direct run. Every role guard below still applies in full.

Residual trust, documented rather than closed:
- The operator's `PATH` (which resolves `git`, `ssh` and the ProxyCommand's
  `gcloud`), `~/.ssh/config`, and the collections installed in
  `~/.ansible/collections` (the `google.cloud` inventory plugin and the
  `ansible.posix` callbacks the repo `ansible.cfg` enables) run with the
  password in the controller environment (Ansible passes its whole
environment to ssh, so the IAP ProxyCommand's `gcloud` inherits it too). Install collections only from
  `infra/ansible/requirements.yml`.
- The script verifies the checkout it runs from, including itself, so a
  modified script can skip its own checks. Use a fresh clone or worktree of the
  reviewed revision and run it unmodified.
- A `uv run --with` package, or other code injected into the interpreter,
  runs before the distribution check can refuse it.
- The operator can still edit files between verification and Ansible reading
  them, or paste the password anywhere else in their own shell.

The role behaves as follows:
- The `enabled` gate is decided exactly once. `tasks/main.yml` captures the
  flag a single time, with no loop item in scope, into a `no_log` fact guarded
  by `default(false, true)`, and hands the work to a dynamic `include_tasks`
  gated on that fact. A lazily templated flag such as `{{ item is defined }}` is
  therefore false and cannot flip to true inside a later loop; `--start-at-task`
  cannot jump into the not-yet-included file to skip the guards and reach the
  write, and starting at the include itself fails on the missing captured fact;
  and a flag whose template renders undefined while reading a secret resolves to
  false without surfacing the value.
- The gate opens only for a real boolean true (`is sameas true`); a string such
  as `"true"` leaves it closed. The `bool` filter is avoided: on ansible-core
  2.21 it prints any non-boolean string it coerces, such as a flag templated to
  the password, in a deprecation warning that `no_log` does not suppress.
- Before it inspects anything, it refuses a password variable and any other
  variable named `coding_hosted_postgres_environment_*` except the `enabled`,
  `confirmation`, `source_revision` and `host` inputs and the captured gate,
  whether set by extra vars, inventory or vars files. Presetting the captured
  gate only enables materialization, which every guard still decides. A preset
  loop `item` is refused too. Both checks list variable names with `varnames`
  and never render a value, because `is defined` would render a raising
  template and print its error. Extra vars outrank registered results and
  set_facts. Without this check, a preset result such as
  `coding_hosted_postgres_environment_units` would disable the live-unit guard,
  and a preset `coding_hosted_postgres_environment_document` would replace both
  the file and the digest used to verify it. The separate removal role's
  `_cleanup_` variables are excluded so its presence never produces a misleading
  refusal here.
- It gathers no facts. Host identity and the worker and custodian accounts come
  from registered `setup` and `getent` probes, because an `ansible_facts` extra
  var replaces gathered facts but not a registered result. It also requires
  `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` and
  `ansible_play_batch == ['ditto-coding-hosted-v2']`, so the play targets
  exactly the reviewed inventory host: the hostname, architecture and
  distribution come from the target's own `setup`, which a labelled rogue VM
  controls, but the inventory names in the play and batch do not, and extra
  vars cannot override either, so a run without `--limit` cannot route the
  password to such a host. Both are pinned because with `serial: 1` the batch
  alone is the reviewed host while a rogue host is still in the play.
- It refuses check mode up front with a constant message, since it only writes
  files and cannot verify a write in `--check`. The dormant fixture stays the
  check path: `main.yml` never includes the write file when the gate is closed.
- Before the password is read it requires pipelining to be genuinely on and
  `keep_remote_files` off. Without pipelining ansible-core writes the module,
  including its arguments, to a temp file under the ssh user's `~/.ansible/tmp`
  on the target, and with `keep_remote_files` on it is left there, so the
  password could persist even on a dropped connection. The playbook sets
  `ansible_pipelining: true` in its play vars: that is the ssh connection
  plugin's own input variable, so it genuinely enables pipelining, whereas the
  repo `ansible.cfg`'s `[ssh_connection] pipelining = True` sets the plugin
  option without populating the variable. On ansible-core 2.21.2 the plugin
  reads `ansible_pipelining` and then `ansible_ssh_pipelining`, the later one
  wins, and both outrank the `ANSIBLE_PIPELINING` environment and ini settings,
  so an extra var `ansible_ssh_pipelining=false` disables pipelining while
  `ansible_pipelining` still reads true. The role therefore requires both to be
  a real boolean true (`is sameas true`, never the `bool` filter, which prints a
  coerced value) and `keep_remote_files`, which has no variable form and
  disables pipelining on its own, to be false. Run the playbook as written,
  without exporting `ANSIBLE_PIPELINING` or overriding either variable.
- Every variable it reads is a documented input, a prefixed name the preset
  check refuses, the refused loop `item`, or a magic variable extra vars cannot
  override. `inventory_hostname` and `group_names` are host variables that an
  extra var replaces, so group membership is proved from `groups` and
  `ansible_play_hosts_all` instead: every host in the play must belong to
  `role_coding_hosted`. Every message is fixed text.
- Every operator input is captured once, with no loop item in scope and with a
  `default(..., true)` guard, so a lazily templated value cannot render one
  thing for a guard and another inside a loop, and a template that errors while
  reading the password becomes an empty string that fails validation instead of
  surfacing the value in a fatal error message. Every later task, and the
  document, use only the captured values.
- It requires a source revision of exactly 40 lowercase hex characters and a
  host address that trimming leaves unchanged. A `$` anchor alone would accept a
  trailing newline.
- It validates a bounded, single-line password without logging it.
- It refuses unless every listed worker or custody unit is `inactive` or
  `failed`. This is an allow-list, so `active`, `activating`, `deactivating`,
  `reloading`, `refreshing` (systemd 256 and later), `maintenance`, a future
  state or an unparseable line all refuse. An empty listing means no such unit
  is loaded and is allowed. The role stops nothing.
- It writes each copy with a role-local module, never `copy`/`file`, so no
  symlink can be followed. A path-based write run as root would follow a
  `private` (or copy) symlink the reader account swaps in, even mid-run: it
  would chown and chmod the link target to the account and write the password
  inside it, and a `follow=false` stat of the final path would still pass. The
  module instead opens every component from `/` with `O_NOFOLLOW`, verifies the
  reader-owned home and `private` directories on the descriptors (creating
  `private` with `mkdirat`+`fchown`+`fchmod`, then re-opening to verify), writes
  an `O_CREAT|O_EXCL` temp with `fchown`/`fchmod`/`fsync` and `renameat`s it into
  the pinned directory, unlinks the temp on any failure, and re-verifies the
  parents after the write. The document is a `no_log` argument, so the module
  never writes its invocation to the target's journal and
  `ansible_inject_invocation` returns nothing sensitive; the write result and
  the digest check are `no_log`.
- The unit state is re-checked after the write and again after verification.
  A unit could start between the pre-write listing and the write, so if any unit
  is no longer `inactive` or `failed` the role fails loudly, warning that a copy
  may have been read mid-rotation; it does not roll the copy back. This narrows,
  but by itself does not eliminate, the check-then-act window (see below).
- It verifies ownership, mode, single link and the SHA-256 of each copy against
  the rendered document, with `no_log`. The stat result carries the checksum, so
  `no_log` on the reinspection and the owner/mode assert is what keeps the
  document digest out of the `-v` output and failure lines. It never reads the
  bytes back to the controller and never prints the values or the digest.
- It starts nothing. Admission still depends on the separately reviewed HBA and
  firewall rules above.

Two residual limitations are accepted, not closed, by design:
- **Check-then-act.** The role stops nothing, so a worker or custody unit could
  start after the final re-check but before a reader opens the copy. The
  operator stops every unit first (the pre-write listing must be `inactive` or
  `failed`); the re-checks catch a unit that starts during the write or verify.
- **Operator-supplied Jinja, direct invocation only.** ansible-core renders a
  trusted `-e`/inventory string on any reference and offers no way to read a
  variable's raw text without rendering it. Capturing each input once behind
  `default(..., true)` confines every input to a single render and turns a
  template that renders undefined, such as `{{ {}[lookup('env', …)] }}`, into an
  empty value. It does not neutralise a template that raises: for example
  `{{ lookup('file', lookup('env', 'DITTO_CODING_PG_PASSWORD')) }}` as `enabled`
  or `host` fails the run closed at the capture, writing nothing, but
  ansible-core 2.21.2 prints the raised message, including the value, through
  the task result's `exception` field, which `no_log` deliberately preserves, on
  the console and in any `ANSIBLE_LOG_PATH` log. Core Jinja offers no construct
  that swallows such an error. **The guarded entry point closes this for the
  supported path:** it builds `enabled`, the confirmation and the revision
  itself, accepts `host` only as a pattern-validated environment value, refuses
  template syntax in every input before Ansible starts, refuses
  `ANSIBLE_LOG_PATH` and every other `ANSIBLE_*` override, and accepts no `-e`.
  The residual remains only for an unsupported direct `ansible-playbook` run,
  where the operator who pastes hostile Jinja into their own command already
  holds the exported password. The write module's `no_log` argument and the
  pipelining guard keep the password off the target's disk and journal, but
  cannot stop an operator who already holds it.

Root tests check the guard structure. With `DITTO_ANSIBLE_REHEARSAL=1` they also
run the real, restructured role through ansible-core 2.21.2 against a temporary
tree, imported statically as the playbook does, under the repo's `ansible.cfg`
and `-v --diff`, with a stand-in password. That rehearsal proves that a lazily
templated gate, a gate templated to the password, a string `"true"` and
`--start-at-task` at the write, the render, a guard or the include write
nothing; that a preset loop `item`, a raising password variable, and an
`inventory_hostname` and `group_names` forged for a host outside the group are
refused; that `-vvv` with `ansible_inject_invocation` prints nothing sensitive; that a lazily templated `host` writes the safe captured address rather
than the loop-time one; that a `private` or copy swapped for a symlink is never
followed (the write module refuses or replaces it with a real file); that a
rogue inventory host (also under `serial: 1`), check mode, kept remote files,
and `ansible_pipelining` or `ansible_ssh_pipelining` overridden to false or to a
string are refused, with pipelining coming only from the playbook's play vars
and the repo `ansible.cfg`, nothing exported; that `--start-at-task` at any `main.yml` task with the captured gate
and registers preset still writes nothing; that preset results and document variables, forged
`ansible_facts`, `refreshing`, `maintenance` and unknown unit states, and
trailing-newline inputs are refused before anything is written; and that no
password form reaches the console or log even when the owner/mode check fails.
It searches for the stand-in in raw, JSON-, YAML- and repr-escaped forms, for a
canary no escaping changes, and for the SHA-1, MD5 and SHA-256 digests of the
password and of the document, and it pins the raising-template residual above
exactly. The infra CI Ansible job runs it.

`ditto/tests/test_coding_hosted_guarded_run.py`, shared with the worker
credential roles, tests the guarded entry point against synthetic git
checkouts: extra arguments, a wrong or dirty revision, untracked and ignored
files, `assume-unchanged`/`skip-worktree` edits, same-size same-mtime edits,
symlink and mode swaps, `GIT_*` redirection, every refused environment class,
each input failure class with a canary that must never be echoed, malformed
specs, and the exact argv, extra vars and child environment, which carry no
secret. It also checks that every real spec matches its playbook's role
defaults, confirmation, marker and environment reads. With the rehearsal gate
it runs the real script under `uv run --locked --script` and ansible-core
2.21.2: the synthetic play receives only the constructed values and marker, a
`PATH` `ansible-playbook` is never used, a rogue host is outside the play, and
the raising-template input, a template host, `ANSIBLE_CONFIG`, a `--with`
package and `-e`/`-vvv` arguments are refused before Ansible starts.

The `role_coding_hosted` group connects through IAP only
(`group_vars/role_coding_hosted.yml`). Every native host role therefore reaches
the private address the same way the Platform PostgreSQL VM is reached.

## Removal and rotation

The default-off `coding_hosted_postgres_environment_cleanup` role removes the
two copies above. Run it only through the guarded entry point described under
[Guarded entry point](#guarded-entry-point), with the reviewed revision:

```bash
# From a fresh, clean checkout of the reviewed revision, with no password exported.
GCP_OSLOGIN_USER=… uv run --locked --script infra/scripts/coding-hosted-guarded-run.py \
  postgres-environment-remove <reviewed 40-hex revision>
```

The guard's `postgres-environment-remove.json` spec builds the JSON boolean
`coding_hosted_postgres_environment_cleanup_enabled: true`, the exact
confirmation `REMOVE NATIVE CODING POSTGRES ENVIRONMENT` and the revision
itself, and refuses to run while `DITTO_CODING_PG_PASSWORD` or
`DITTO_CODING_PG_HOST` is exported. The role needs no secret and never reads
`DITTO_CODING_PG_PASSWORD`. Direct `ansible-playbook` invocation is unsupported;
the role's second included task requires the guard's
`DITTO_CODING_HOSTED_GUARDED_RUN=postgres-environment-remove` marker, which is
an accident guard only, since anyone able to run `ansible-playbook` can set it.

It unlinks only the two literal file paths. It never removes, creates or
changes a directory, sibling file, custody key, receipt or evidence record, and
has no path input, glob or recursion.

The gate and the inputs are frozen once:
- `tasks/main.yml` renders the `enabled` flag a single time into a `no_log`
  fact and hands the work to a dynamic `include_tasks`, evaluated once with no
  loop item in scope. A block-level `when:` is re-evaluated for every task and
  loop item, so a flag such as `{{ item is defined }}` used to be false for
  every guard and true inside the removal loops. `--start-at-task` cannot jump
  into the not-yet-included `tasks/remove.yml` to skip the guards, and starting
  at the include itself fails on the missing frozen fact.
- The gate opens only for a real boolean true (`is sameas true`), after
  `default(false, true)`. The `bool` filter is avoided: on ansible-core 2.21 it
  prints any non-boolean string it coerces, such as a flag templated to a
  secret, in a deprecation warning that `no_log` does not suppress.
- `confirmation` and `source_revision` are frozen once, under `no_log`, with
  `default(..., true)`, which turns an undefined result, including one produced
  while reading a secret (for example `{{ {}[lookup('env', …)] }}`), into an
  empty value that fails validation. Every later task reads only the frozen
  values. The role never renders an input into a message: every refusal is
  fixed text, and only the partial-removal failure and the final report render
  the frozen revision, after the host check has proved it is exactly 40
  lowercase hex characters, and paths from registered results.

Before removing anything it refuses when:
- any variable named `coding_hosted_postgres_environment_cleanup_*` other than
  the `enabled`, `confirmation` and `source_revision` inputs and the frozen gate
  is set, from extra vars, inventory or vars files. The check lists names
  without rendering values and runs before any fact is frozen or result
  registered. Extra vars outrank registered results and facts, so a preset
  result such as `coding_hosted_postgres_environment_cleanup_units` would
  otherwise replace the unit listing and disable its guard. Presetting the
  frozen gate only enables removal, which every guard still decides. The
  materialization role's `coding_hosted_postgres_environment_*` names never
  match this prefix, and that role excludes these names in turn. A preset loop
  `item` is refused too. Every other variable the role reads is a magic variable
  extra vars cannot override;
- the machine is not the dedicated Debian 13 x86_64 host
  `ditto-coding-hosted-v2`, any host in the play is outside `role_coding_hosted`,
  or the play targets more than the reviewed host. The playbook gathers no
  facts: identity comes from a registered `setup` probe, because an
  `ansible_facts` extra var replaces gathered facts. Membership is read from
  `groups` and `ansible_play_hosts_all`, and the target is pinned with both
  `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` and
  `ansible_play_batch == ['ditto-coding-hosted-v2']`, because
  `inventory_hostname` and `group_names` are host variables an extra var
  replaces while the play, batch and group lists are not: a labelled rogue VM
  run without `--limit` is refused, including under `serial: 1`, where the batch
  alone would be the reviewed host;
- the enabled flag, re-asserted raw inside the include, is not a boolean true.
  The frozen gate could be preset while `--start-at-task` skips the freeze, so
  the include re-checks the raw flag;
- the source revision is not exactly 40 lowercase hex characters (a trailing
  newline is refused) or the confirmation differs;
- any worker or custody unit in the materialization listing has an ACTIVE state
  other than `inactive` or `failed`. This is an allow-list, so `active`,
  `activating`, `deactivating`, `reloading`, `refreshing` (systemd 256 and
  later), `maintenance`, a future state or an unparseable line all refuse. An
  empty listing means no such unit is loaded and is allowed. The role stops
  nothing;
- a reader home or `private` directory is a symlink, not a directory, not owned
  by its reader, or writable by group or others;
- a copy path, inspected without following links, is anything except absent
  or a regular, single-link file owned by its reader. A symlink, directory,
  hard link or another account's file needs manual reconciliation.

The unlink cannot follow a link or remove a directory. Each reader home and
`private` directory is created `0700` and owned by its reader (`coding_hosted`,
`coding_hosted_custody_key` and the materialization role), so the unprivileged
reader could swap a path component for a symlink after the inspection, and a
path-based unlink run as root would follow it. The role-local
`coding_hosted_postgres_environment_unlink` module therefore never resolves the
path as a string. It opens every component from `/` with `O_NOFOLLOW` and
`O_DIRECTORY`, re-checks that the home and `private` directory are owned by its
reader and not group- or other-writable and that the copy is a regular
single-link file owned by its reader, and removes the entry with `unlinkat`
relative to the pinned `private` directory. `unlinkat` without `AT_REMOVEDIR`
cannot remove a directory and never follows a symlink, so a later swap can at
most remove the reader's own directory entry. Directories above the homes are
not trusted for this: a swapped ancestor can only lead to a directory the reader
itself owns.

The unit state is re-checked after the unlink. A unit could start between the
first listing and the unlink and read a copy mid-removal; if any unit is then no
longer `inactive` or `failed`, the role fails loudly and does not restore the
copy.

No task handles the password, and the module's only arguments are a literal
path and owner, so no module invocation written to the target's journal, and
nothing `ansible_inject_invocation` returns, can carry it.

It inspects metadata only: no checksum, slurp or fetch. It attempts both
unlinks; if either fails, it fails with the source revision and the exact paths
removed and not removed, so a partial removal is never silent. The report's
`removed=` and the partial-failure message are built from the module's returned
state, never the pre-unlink stat, so a copy that survived under a parent renamed
between the inspection and the unlink is never claimed removed; such a vanished
copy fails the run loudly. It then verifies that both paths are absent. A re-run
reports both as already absent. `--check` runs the same descriptor checks and
lists what would be removed.

One residual is accepted for unsupported direct runs only. `default(..., true)`
neutralises a template that renders undefined, not one that raises. A template
that raises with a secret in its message, for example
`{{ lookup('file', lookup('env', 'DITTO_CODING_PG_PASSWORD')) }}` as `enabled`
or `source_revision`, fails the run closed at the freeze, but ansible-core
2.21.2 prints the raised message through the task result's `exception` field,
which `no_log` deliberately preserves, on the console and in any
`ANSIBLE_LOG_PATH` log. Core Jinja offers no construct that swallows a raised
lookup or filter error, and there is no way to read a variable without rendering
it. The guarded entry point closes this for the supported path: it accepts no
`-e`, builds every extra var itself, refuses `ANSIBLE_LOG_PATH` and every other
`ANSIBLE_*` override, and refuses an exported password outright. A direct run
still needs the template written into the operator's own command line or a
reviewed inventory, with the secret already readable on the controller. The
rehearsal pins this residual exactly.

Root tests check the role structure and exercise the unlink module directly,
including a parent swapped for a symlink after pinning and a copy swapped for a
directory or symlink between inspection and removal. With
`DITTO_ANSIBLE_REHEARSAL=1` they also run the real role and module through
ansible-core 2.21.2 against temporary trees, under the repo's `ansible.cfg` and
`-v --diff`, with a stand-in password exported and planted in the copies. The
rehearsal covers lazily templated and lookup-based gates and inputs, extra vars
that preset a result, forge identity, the gate or a loop `item`, an
`inventory_hostname` and `group_names` forged for a host outside the group,
`--start-at-task` at the unlink, the copy inspection, a guard and the include,
any `main.yml` task with the frozen gate and registers preset, a rogue inventory
host, `-vvv` with `ansible_inject_invocation`, a unit that starts during removal,
and every refusal above. It searches the console and log for the stand-in in raw, JSON-,
YAML- and repr-escaped forms and for the SHA-1, MD5 and SHA-256 digests of the
password and of the copy document. The infra CI Ansible job runs it.

### Removal is not revocation

Removing the two copies does not revoke any credential. The `ditto` password
stays valid wherever it is held, including retained runtime roots, and the HBA,
UFW and network rules follow the rollback steps above. Services configured to
read these copies fail closed until the files are materialized again. Treat a
suspected exposure of any holder below as a leak of the shared `ditto` role
password: rotate that password as described below. Running cleanup alone is
never a leak response.

### Holders of the `ditto` password

On the Coding host, the complete `POSTGRES_*` list, including
`POSTGRES_PASSWORD`, is held in:
- `/var/lib/ditto-coding-custody/private/postgres-environment.json`, owned by
  `ditto-coding-custody`, mode `0600`, read by the custody service through its
  `postgres_environment_file`;
- `/var/lib/ditto-coding-hosted/private/postgres-environment.json`, owned by
  `ditto-coding-hosted`, mode `0600`, read by the hosted runtime through its
  protected configuration's `postgres_environment_file`;
- `<runtime_root>/postgres.json` for every hosted runtime invocation, including
  each attempt of a bounded rollout. `write_worker_config` in
  `apps/platform/ditto/api_server/coding_hosted_runtime.py` writes it as an
  exclusive mode-`0600` file owned by `ditto-coding-hosted` for the Go worker's
  start helper. Runtime roots are retained evidence and reconciliation state:
  nothing deletes them automatically, including after a failure. This role
  neither finds nor removes them, and must not be extended to;
- any other protected configuration whose `postgres_environment_file` names a
  separate copy, for example one written for the evidence recovery or canary
  acceptance commands. This role does not find those either;
- `~/.ansible/tmp/ansible-tmp-*` of the ssh user, only for a materialization
  run made without pipelining or with `keep_remote_files` on, which uploads the
  module and its password argument there and can leave it behind on an
  interrupted connection. The materialization role now refuses both before the
  password is read, and the playbook enables pipelining, so a current run writes
  nothing there; check for leftovers from older or modified runs. This role does
  not remove them.

While services run, the Go worker also passes the entries to the Python start
helper as process environment, and each running Platform process holds the
password in memory. Stopping the services ends both.

Outside the Coding host:
- the GitHub Actions secret `PLATFORM_DB_PASSWORD` in the `infra-plan`
  environment. `.github/workflows/infra-plan-apply.yml` passes it to the
  `gcp-platform` plan as `TF_VAR_db_password`;
- the Secret Manager secret `platform-db-password`, whose version
  `infra/terraform/stacks/gcp-platform` writes from `var.db_password`. The value
  is also stored in that root's Terraform state
  (`gs://ditto-app-dev-tfstate/gcp-platform`) and in each sealed plan under
  `gs://ditto-app-dev-tfstate/ci-plans/gcp-platform/` until apply removes it. A
  plan that is never applied stays there;
- the Platform PostgreSQL VM: the `ditto` role, which serves both
  `ditto_platform_dev` and `ditto_platform_prod`, and
  `/opt/ditto/secrets/postgres-ditto.password` (`postgres:postgres`, `0640`),
  both set by `gcp-platform-pg.yml` and `roles/postgres`. A converge without
  `DITTO_PG_PASSWORD` keeps the existing password; exporting a new value there
  changes it;
- each Platform app VM's `apps/platform/.env` (`POSTGRES_PASSWORD`), which
  `roles/platform_app` renders from `platform-db-password` for both Platform
  environments;
- an operator controller, only while `TF_VAR_db_password`, `DITTO_PG_PASSWORD`
  or `DITTO_CODING_PG_PASSWORD` is exported for a protected run.

### Rotation

The cleanup role does not rotate the shared `ditto` PostgreSQL role password.
Rotation is this ordered procedure, and it is not complete without step 2:
steps 3 to 5 alone only replace two files with the same, still-valid password.

1. **Stop.** Stop the Coding worker and every custody instance through their
   own reviewed procedure, after reconciling unfinished attempts and evidence.
   Nothing here stops or starts them. The cleanup listing must show only
   `inactive` or `failed` units.
2. **Rotate the `ditto` password.** This is a separate protected change that
   is not implemented here. It must move every off-host holder above to one
   new value: the `PLATFORM_DB_PASSWORD` GitHub secret; `platform-db-password`
   through a reviewed `gcp-platform` plan and apply; the PostgreSQL role and
   protected password file through `gcp-platform-pg.yml` with the new
   `DITTO_PG_PASSWORD`; and the Platform app `.env` for both environments
   through `gcp-platform-app.yml`, followed by a reviewed app restart.
   Platform impact: once the role password changes, new database connections
   from both the dev and prod Platform APIs fail until their `.env` is
   re-rendered and the apps restart. Existing sessions stay authenticated.
   Schedule it as a maintenance window for both environments. Its own
   verification must show that the previous password no longer authenticates.
3. **Clean up.** Run the guarded `postgres-environment-remove` operation with
   the reviewed source revision, without exporting any password (the guard
   refuses one).
4. **Re-materialize.** Export the new value from `platform-db-password` only in
   the controller environment as `DITTO_CODING_PG_PASSWORD`, with
   `DITTO_CODING_PG_HOST`, run the guarded `postgres-environment-materialize`
   operation and unset both.
5. **Verify.** Before starting anything, confirm that the materialization's
   owner, mode and digest checks passed, that the cleanup report names the
   reviewed source revision, that both Platform APIs connect, and that a
   bounded native-service database query from the Coding host succeeds with
   the new copies. Then start services through their own
   reviewed procedure.

Runtime roots written before step 2 still contain the previous password. After
step 2 it no longer authenticates, but the files stay retained evidence. Decide
their retention or disposal as a separate evidence change, never through this
role.

No automated database password rotation exists for this path.
