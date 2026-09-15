# Hosted-v2 control signer: host wiring, isolation and protected ceremony

This layer wires the existing default-off Platform signer loader
(`apps/platform/docs/coding-hosted-control-startup-v2.md`) and the validator
control command (`docs/coding-hosted-validator-transport-v2.md`) into host
convergence and the Platform deploy. It follows the 2026-09-15 decision for
#1901: a dedicated, locked-down `ditto-api` service identity is the only
component that may read the online control-signer seed, and the deploy user's
Docker and journalctl access is narrowed whatever the signer mode.

Every switch ships off, and nothing here is active on any host. This layer
never generates, transports, backs up or activates a key. No CI job, workflow
input, workflow artifact, Git object, chat message (including Telegram),
command argument or log line contains seed material.

## The key

The control signer is a new **online operational** SR25519 key held by the
Platform API process. It signs canonical native-v2 status and result envelopes
and nothing else. It is completely separate from the offline curator Ed25519
key, which never touches this host or this automation. It is also distinct from
every validator hotkey and coldkey, from the screener hotkey and from any
payment wallet. A signature never makes an operation weight-eligible.

Neither seed, online or curator, may ever be placed in CI, Git, Telegram,
workflow artifacts, command arguments or logs.

## Who can read the seed

| Identity | Runs | Seed |
|---|---|---|
| `ditto-api` (system user, no login, no sudo, no supplementary group) | only the `ditto-api` API process, through `ditto-platform-api.service`, and its metadata preflight | **Owner.** The only non-root identity that can open it |
| `ditto-api-build` (system user, no login, no sudo, no supplementary group) | `uv sync` for sealed releases, in a sandboxed transient unit | No |
| `deploy` | `scripts/update.sh`, the pm2 relays `ditto-api-relay-1/2`, `ditto-screened-image-cleanup`, migrations | No. It reaches `ditto-api` only through four exact sudo commands |
| team accounts in `ditto` | `sudo -u deploy`, `systemctl start/stop/restart ditto-*`, journal reads | No |
| root, including OS Login admins and the Deploy Platform workflow identity | everything | Yes (see [Residual risks](#residual-risks)) |

`read_private` and `private_directory` compare the owner of the seed and its
directory with the effective UID of the reading process, so a placement owned
by `ditto-api` fails closed for any other process even if its permissions were
widened. The Ansible guard requires the same owner, and it accepts only root or
`ditto-api` as owners of the ancestors. A `deploy`-owned seed, directory or
ancestor fails the converge.

The signer may be enabled only together with both isolation switches below. The
Ansible profile guard and `scripts/update.sh` both refuse an enabled signer
while `ditto-api` would run as `deploy`.

## Switches

| Variable (`platform_app`) | Default | Effect when true | Kind |
|---|---|---|---|
| `platform_pylon_root_unit_enabled` | `false` | Pylon through a root-owned unit; `deploy` leaves the `docker` group | Reviewed activation |
| `platform_api_service_identity_enabled` | `false` | `ditto-api` as the `ditto-api` user from sealed releases | Reviewed activation |
| `platform_coding_hosted_control_enabled` | `false` | Renders the signer settings; requires both switches above and a `ditto-api`-owned seed | Reviewed activation |
| team journal and sudoers narrowing (`base` role) | always | See [Deploy access](#deploy-access) | **Immediate** on the next `base` converge |

`ditto/tests/test_coding_hosted_control_signer_wiring.py` fails when any of the
three activation variables, or `validator_stack_coding_hosted_control_enabled`,
is set truthy in an inventory, playbook or workflow file that is not listed in
its `REVIEWED_ACTIVATIONS` table. An activation pull request adds its host_vars
file there in the same reviewed change.

With every switch off, a converge and a deploy behave as before: pm2 runs
`ditto-api` as `deploy` from `/opt/ditto-subnet`, `update.sh` runs
`docker compose` as `deploy`, and nothing new is created. The only visible
differences are the two `.env` lines `DITTO_PLATFORM_API_SUPERVISOR=pm2` and
`DITTO_PLATFORM_PYLON_UNIT=`, which select exactly that path.

## Deploy access

### Immediate: exact team sudo rules, none for deploy

`deploy` is a member of `ditto`, so it had every `%ditto` rule in
`/etc/sudoers.d/ditto-team` (`roles/base/tasks/users.yml`). They gave root:

- `/bin/journalctl *` accepted any option as root, for example `--cursor-file=`
  (writes a root-owned file at any path), `--vacuum-*`, `--setup-keys`, and the
  pager, which has a shell escape wherever systemd's secure pager mode is not in
  effect;
- `/bin/systemctl status ditto-*` ran the same pager as root;
- `(ALL) ... systemctl start|stop|restart ditto-*`: in sudoers a `*` matches any
  further arguments, so it allowed extra units (`stop ditto-x docker.service`),
  pager-opening options, stopping isolation guards such as the coding executor
  egress guard, and any run-as user.

The rules are now one alias of exact argument vectors,
`/bin/systemctl start|stop|restart <unit>`, for a reviewed list of 15 service
units (each with and without `.service`) and 6 timers, plus `daemon-reload` and
`reload caddy`. They run as `(root)` only and are granted to `%ditto,!deploy`,
so `deploy` gets none of them. Isolation guards are not in the list: the coding
executor and hosted egress units, the sandbox firewall, the IMDS guard, the
egress proxy and rootless Docker daemons. `%ditto ALL=(deploy) NOPASSWD: ALL`
stays. `visudo` parses the file in tests.

Team members join `systemd-journal` and run `journalctl -u <unit>` and
`systemctl status <unit>` without sudo, as themselves. `deploy` is not a member.
Its only journal read is the exact Pylon rule below. Operators with OS Login
admin keep their own full sudo, which the skill scripts use.

No automation used any removed rule: `update.sh`, the Deploy Platform workflow,
the relay release and the validator and screener services never call
`systemctl` or `journalctl` through the `ditto` rules. This part is therefore
not gated. It takes effect on the next converge of any playbook that runs the
`base` role, and no such converge has been run. A team member who restarts a
unit outside the list needs OS Login admin or a reviewed addition.

### Gated: Pylon without the docker group

The `docker` group is root-equivalent. `deploy`'s only use of it is
`update.sh`'s `docker compose up -d --wait pylon` (also in `start.sh`). The
relays, the cleanup job and `ditto-api` never talk to a Docker daemon on the
Platform host; `builder_image.py` calls the Artifact Registry API through
`gcloud`, and `coding_bounded_rollout.py` runs on the separate rootless coding
host.

A root unit that ran compose on the checkout's `docker-compose.yml`, or with
`deploy`'s `.env`, would still hand `deploy` root, because `deploy` can edit
both. With `platform_pylon_root_unit_enabled` (`tasks/pylon_root_unit.yml`) the
role:

1. installs `/etc/ditto-platform/pylon/compose.yml` (root, `0600`). Its `pylon`
   service is byte-for-byte equal in YAML to `apps/platform/docker-compose.yml`,
   and `ditto/tests/test_platform_deploy_access.py` fails on drift;
2. renders `/etc/ditto-platform/pylon/pylon.env` (root, `0600`) with the
   `SUBTENSOR_NETWORK` and `PYLON_OPEN_ACCESS_TOKEN` compose used to interpolate
   from the exported `.env`. The other `pylon` variables keep their compose
   defaults, as before, except the wallet mount source. That was
   `/home/deploy/.bittensor/wallets`; dockerd resolves a bind source as root, so
   `deploy` could have pointed it at any host path with a symlink. It is now
   `BITTENSOR_WALLET_PATH=/etc/ditto-platform/pylon/wallets`, an empty
   root-owned directory. The Platform's Pylon serves reads only
   (`PYLON_IDENTITY_TOKEN` is empty), and the converge refuses when deploy's old
   wallet path is a link, not a directory, or not empty. Changing the mount
   source recreates the container once;
3. installs `ditto-platform-pylon.service`, a root `oneshot` that runs
   `docker compose --project-name ditto-platform ... up --detach --wait pylon`
   with an empty root-owned `DOCKER_CONFIG`. It is not enabled; the container's
   `restart: unless-stopped` still restores Pylon after a reboot;
4. adds `/etc/sudoers.d/ditto-platform-pylon` with two exact rules:
   `systemctl restart ditto-platform-pylon.service` and
   `journalctl --no-pager --quiet --output=short-iso --lines=80 --unit=ditto-platform-pylon.service`.
   The second lets `update.sh` print the unit's journal when it fails. It is a
   separate rule, not part of the release installer's `logs`, because the Pylon
   unit can be enabled without the dedicated identity;
5. renders `DITTO_PLATFORM_PYLON_UNIT=ditto-platform-pylon.service`, so
   `update.sh` and `start.sh` restart the unit instead of calling compose;
6. removes `deploy` from `docker` with `gpasswd --delete`.

This is gated because it cannot be proven without a live host. The new mount
source recreates `ditto-platform-pylon-1` once, and the pm2 daemon keeps its old
supplementary groups until it restarts. The signer does not trust the switch for
that last point: see [the live Docker check](#live-docker-check).

### Live Docker check

The switch changes files, but Docker access lives in running processes: the
`deploy` pm2 daemon and its children keep the `docker` group until
`pm2-deploy.service` restarts, so `pm2 start -- docker run -v /etc/ditto-platform:...`
would still work. `files/deploy-docker-access.py` checks the live host as root
and fails closed:

- for the `docker` group and for whichever group owns `/run/docker.sock`, deploy
  is not a member and it is not deploy's primary group;
- no process whose real, effective, saved or filesystem UID is deploy holds such
  a GID (read from every `/proc/<pid>/status`);
- the socket is owned by root, grants nothing to other users, and has no POSIX
  ACL.

It runs in the signer converge guard (after the stat guard), in the release
installer's `install` and `activate` whenever the root-owned environment enables
the signer, and, as an early advisory stop, in `update.sh` before the install.

## Dedicated `ditto-api` identity

`platform_api_service_identity_enabled` (`tasks/api_service_identity.yml`)
prepares the identity. It does not enable or start a unit, build a release or
touch the seed.

- **Users.** `ditto-api` and `ditto-api-build` are system users with
  `/usr/sbin/nologin`, a locked password and exactly no supplementary group. The
  converge fails if either is in `docker`, `sudo`, `adm`, `systemd-journal`,
  `google-sudoers`, `ditto` or `deploy`'s group.
- **Root-owned inputs.** `/usr/local/sbin/ditto-platform-api-release` (installer),
  `/usr/local/libexec/ditto-platform-api/launch` (launcher),
  `/etc/systemd/system/ditto-platform-api.service`, and
  `/etc/ditto-platform/api/` (root:`ditto-api`, `0750`). That directory holds:
  - `platform.env`, the same template and secrets as `deploy`'s `.env`, readable
    only by `ditto-api`, with paths pointing into the sealed release;
  - `release.json` (interpreter and deploy user);
  - a root-only copy of the read-only GitHub deploy key, and a root-owned
    `known_hosts` for github.com.
- **Sudoers.** `/etc/sudoers.d/ditto-platform-api` allows `deploy` exactly
  `ditto-platform-api-release install`, `... activate`, `... stop` and
  `... logs`. There is no wildcard, so sudo refuses any other argument. The
  installer reads its request from stdin and never lets `deploy` choose a path,
  user, unit or command. The only file it reads from `deploy`'s tree is the
  built dashboard, as data.
- **Relays, cleanup job, migrations.** They stay on `deploy`'s pm2 and checkout.
  Caddy still proxies `localhost:8000`.

### Process supervision

`ditto-platform-api.service` runs as `User=ditto-api`, `Group=ditto-api` with an
empty `SupplementaryGroups=`. It has no root pm2 daemon and no second pm2 home:

- `ExecStart=launch serve` is one launcher run. It resolves `current` once, runs
  `python -I -m ditto.api_server.coding_hosted_signer_preflight --check-metadata`
  from that release as `ditto-api`, stops on failure, then execs
  `python -I -m ditto.api_server` from the same release. There is no separate
  `ExecStartPre` that could resolve `current` differently.
- The launcher refuses any user but `ditto-api`. It
  requires `/opt/ditto-platform-api/releases/<40-hex>`, sources only
  `/etc/ditto-platform/api/platform.env` and `deploy.env`, and exports
  `DITTO_BUILD_COMMIT=<revision>` so `/health` reports the sealed revision.
- Hardening: `NoNewPrivileges`, empty `CapabilityBoundingSet` and
  `AmbientCapabilities`, `ProtectSystem=strict`,
  `ReadOnlyPaths=/opt/ditto-platform-api /etc/ditto-platform`,
  `InaccessiblePaths=` the deploy checkout, the legacy checkout, relay releases,
  the installer's Git workspace, the builder's cache and the Docker socket.
  Also `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`,
  `ProtectControlGroups`, `ProtectClock`, `ProtectHostname`,
  `ProtectProc=invisible`, `RestrictNamespaces`, `RestrictRealtime`,
  `RestrictSUIDSGID`, `LockPersonality`, `RemoveIPC`,
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`,
  `SystemCallArchitectures=native` and
  `SystemCallFilter=@system-service ~@privileged`. The state directory
  `/var/lib/ditto-platform-api` (`0700`) is the `HOME` for `gcloud`. Only the
  Hippius evidence spool is added to `ReadWritePaths`, and only when Hippius
  evidence is enabled. `systemd-analyze security --offline` rates the rendered
  unit 1.4 ("OK").
- pm2 parity: `KillSignal=SIGINT`, `TimeoutStopSec=35`, `MemoryMax=3072M`,
  `Restart=on-failure` with a start limit of 10 in 300 s (pm2's
  `max_restarts: 10`), and `LimitNOFILE=65536`.

### Code integrity: sealed releases

A user split alone would not help. `deploy` writes `/opt/ditto-subnet` and its
`.venv`, so it could change code that the next restart runs as `ditto-api`.
Instead, `ditto-api` runs only from sealed root-owned releases.
`ditto-platform-api-release install` does the following as root:

1. Clones `main` of `git@github.com:ditto-assistant/ditto-subnet.git` into a
   fresh root-only directory, commits only (`--filter=tree:0`), and refuses the
   revision unless `git merge-base --is-ancestor <revision> main` holds.
   Unmerged branches and pull-request heads never run as `ditto-api`.
2. Fetches that exact revision (`--depth=1`) into a second fresh repository.
   Git verifies every object hash on receipt, and the revision must resolve to
   itself. Git runs with no system or global configuration, `GIT_ALLOW_PROTOCOL=ssh`,
   and `ssh -F /dev/null` with the root-owned key and `known_hosts`
   (`StrictHostKeyChecking=yes`, `BatchMode=yes`). Nothing is read from
   `deploy`'s checkout or `.git`.
3. Extracts `git archive` into `/opt/ditto-platform-api/releases/<revision>`
   with Python's `data` tar filter, which refuses absolute or escaping names and
   links, devices and set-id bits.
4. Copies `apps/platform/dashboard/dist` from the deploy checkout as plain data.
   Each path component is opened without following links, and each entry must
   be a single-link regular file or directory owned by `deploy`, within file
   count, size and depth bounds. A link, a special file, or a file `deploy` does
   not own (such as the seed) fails the install instead of being published.
5. Builds the environment as `ditto-api-build` in three transient `systemd-run`
   units. Each has the same sandbox plus `ReadWritePaths=` only for its `.venv`
   and cache:
   - `uv venv`;
   - `uv pip install --require-hashes --no-build -r apps/platform/release-build-requirements.txt`,
     the reviewed, hash-pinned build backends (hatchling, hatch-vcs, editables
     and their requirements) as wheels only;
   - `uv sync --frozen --no-dev --no-build-isolation`.

   The sync builds the project, the shared protocol and the pinned
   bittensor-pylon-client Git dependency with exactly those backends, never
   resolving one from an index, then removes them. Runtime packages are verified
   against `uv.lock`. It uses the pinned root-owned interpreter
   (`platform_api_release_python`, default `/usr/bin/python3.13`),
   `UV_PYTHON_DOWNLOADS=never`, `UV_LINK_MODE=copy` and
   `UV_COMPILE_BYTECODE=1`. Each unit's cgroup is stopped when uv exits, so no
   builder process outlives the build. A local rehearsal against a read-only
   source tree showed nothing outside `.venv` is written.
6. Seals the tree: every entry root-owned, directories `0755`, files `0644` or
   `0755`. It refuses hard links and special files. It resolves every link hop
   by hop and refuses any whose resolution leaves the release at any step, so a
   chain of individually harmless relative links cannot escape. The only
   exception is `.venv/bin/python*` ending at the pinned interpreter.
7. Writes a receipt with the SHA-256 of a manifest over every path, owner, mode,
   content digest and link target.
8. Runs the metadata preflight from the sealed release as `ditto-api`, in a
   sandboxed transient unit. Only then does it stage the deploy-owned values
   (the seven `.env.deploy` keys, validated against a strict character set and
   shell-quoted) as `deploy-<revision>.env`, root:`ditto-api` `0640`.

`activate` re-verifies the whole manifest, moves the staged values to
`deploy.env`, atomically points `current` at the release, and runs
`systemctl enable`, `systemctl reset-failed` (so a unit that hit its start limit
does not block a rollback) and `systemctl restart`. The manifest is hashed once
when sealed and once per activation, plus once when `install` reuses a release. It keeps the running and previous
releases and deletes older ones. A release that fails its manifest is rebuilt,
unless it is the running one, which is never deleted. `stop` runs
`systemctl disable --now`. `logs` prints the unit's last 80 journal lines with
`--no-pager`.

Trade-offs:

- Each deploy clones `main` history (commits only) and exports the revision
  from GitHub, and builds a second environment. A reused release skips both.
  That costs deploy time, a GitHub dependency, and disk for up to two releases
  of about 90 MB of source plus the environment on a 30 GB boot disk.
- The dashboard bundle is built by `deploy` and copied as data. `ditto-api`
  serves those bytes but never executes them, so `deploy` still controls what
  browsers load, as today.
- Runtime dependencies are pinned by `uv.lock` hashes, build backends by
  `release-build-requirements.txt` hashes. Changing either is a reviewed commit
  on `main`. Backends run as `ditto-api-build`, which holds no secret and cannot
  write a sealed tree, but their output lands in the environment `ditto-api`
  imports, so they are part of the reviewed code.

### What automation does

| | Every switch off (default) | Pylon unit and identity on, signer off | All three on |
|---|---|---|---|
| `platform_app` converge | As before; signer tasks skipped, the seed path never inspected | Adds the Pylon unit and the identity as above. No unit started | Profile guard (hotkey, both switches, `platform_api_process_user == 'ditto-api'`), then the stat-only guard on the fixed path with `follow: false` and `get_checksum: false`, then the live Docker check |
| `update.sh` | pm2 as `deploy`, compose as `deploy`, no sudo | `install` before Pylon and migrations; the pm2 copy of `ditto-api` is removed and `pm2 save`d before `activate`, which runs after migrations; an activation failure reports the unit state and journal; verify through `systemctl show` and `/health` | The same, after the advisory live Docker check. The metadata preflight runs as `ditto-api` inside `install` and again in the unit's launcher. `deploy` never stats or opens the seed |
| Enabled signer while `ditto-api` would run under pm2 | — | — | Converge fails at the profile guard; `update.sh` stops at stage `signer-preflight` before Pylon, migrations or pm2, and rolls the checkout back |
| `validator_stack` converge | Trust off and empty | Same | Unchanged: exact SS58 checks, then render |

No task creates, copies, templates, fetches, slurps, hashes, moves or deletes
the seed. Neither the Ansible guard nor the preflight reads it, so neither can
catch a hotkey that does not match it or an all-zero seed. Only API startup
catches those.

### How `ditto-api` picks up a change

- A converge rewrites `/etc/ditto-platform/api/platform.env` and `.env` but
  restarts nothing.
- Under the unit, every start sources the current root-owned files. That
  includes automatic restarts and reboots, because the unit is enabled by
  `activate`. So a converge that changes the signer settings takes effect at the
  next restart, even without a deploy. Change signer settings only together with
  the deploy that follows them (see the orders below).
- Under pm2 (switches off), the old behaviour holds: pm2 reuses the environment
  from the last `--update-env` and `pm2 save`.

## Protected ceremony (outside this repository)

Seed creation, backup, placement and production trust activation are a separate
protected ceremony run by the key custodians. This document fixes only the
properties the ceremony must meet:

- Generate the seed on an offline or otherwise protected machine the
  custodians control. Never in CI, a workflow, a workflow artifact, Git, chat
  (including Telegram), tickets, a command argument, a log or this repository's
  automation.
- Use fresh randomness for this key alone. Never derive or reuse it from the
  offline curator key, a validator or screener key, a mnemonic or any wallet.
- Record only the derived public SS58 address for review.
- Place the seed directly at the fixed path, created exclusively rather than
  edited in place. Leave no intermediate copy on another host, image, candidate
  or validator host, shell history or temporary file.
- Custodians hold the backup under their own custody policy and rehearse
  recovery. The backup never enters automation.
- Verify with metadata only. Never print or hash the seed in shared output.

## Fixed placement on the Platform host

| Item | Requirement |
|---|---|
| Seed file | `/etc/ditto-platform/coding-hosted-signer/seed`, fixed, not configurable |
| Content | Raw 32-byte SR25519 seed. No hex text, mnemonic, seed URI or wallet file |
| File | Regular file, single link, mode `0600`, owner `ditto-api` |
| Directory | `/etc/ditto-platform/coding-hosted-signer`, real directory, mode `0700`, owner `ditto-api` |
| Ancestors | `/etc/ditto-platform`, `/etc`, `/`: real directories owned by root (or `ditto-api`), no group or world write. Never `deploy` |

The path is a literal in `platform.env.j2` and in the import vars of
`roles/platform_app/tasks/coding_hosted_signer.yml`. It is not a role default
or inventory variable.

## Enabling order

Each numbered step is a separate reviewed change or operator action. None is
authorized by this pull request.

1. **Pylon unit.** Set `platform_pylon_root_unit_enabled: true` in
   `infra/ansible/host_vars/ditto-platform-<env>.yml`, add its
   `REVIEWED_ACTIVATIONS` entry, and converge
   `gcp-platform-app.yml --limit ditto-platform-<env>`. Run the Deploy Platform
   workflow for the revision in service; `update.sh` restarts the unit, which
   recreates the Pylon container once on the root-owned wallet mount. Restart
   the pm2 daemon in a maintenance window (`systemctl restart pm2-deploy`). The
   signer converge in step 4, the installer and `update.sh` all refuse until no
   `deploy` process holds the `docker` group.
2. **Identity, signer off.** Set `platform_api_service_identity_enabled: true`
   with its entry, and converge. Run the Deploy Platform workflow. It builds and
   activates the first sealed release and removes the pm2 copy of `ditto-api`.
   Verify `/health` on the revision. Also verify Targon candidate promotion
   (`skopeo`), `gcloud` calls, runtime profiles and, if enabled, the Hippius
   evidence spool (its authority files move to `ditto-api` ownership). Rehearse
   on `ditto-platform-dev` first.
3. **Ceremony.** Place the seed owned by `ditto-api` and record the public
   address.
4. **Signer.** Set `platform_coding_hosted_control_enabled: true` and
   `platform_coding_hosted_signer_hotkey: <public SS58>`, add the entry, and
   converge. The profile and stat guards run before any other `platform_app`
   task.
5. Run the Deploy Platform workflow for the revision in service. `install` runs
   the preflight as `ditto-api`, then `activate` restarts the unit. Startup
   derives the key, compares it with the hotkey and runs a sign/verify challenge.
6. Verify through Backroom `get_coding_control_plane` that
   `hosted_control_configured=true`, `shadow_only=true` and
   `weight_eligible=false`.
7. Reviewed validator trust change, for example in
   `infra/ansible/host_vars/ditto-validator-prod.yml`:
   `validator_stack_coding_hosted_control_enabled: true` and
   `validator_stack_coding_hosted_platform_hotkey: <same public SS58>`, plus its
   entry. Take the address from the reviewed Platform change or the custodian
   record, never from a Platform response. Converge
   `gcp-validator-prod.yml`.

If the hotkey reviewed in step 4 does not match the seed, the preflight still
passes, the unit fails its start limit after `activate`, and the deploy fails at
`verify` with the API down. Recover with steps 1 to 3 of Revocation.

## Rotation

There is no hotkey fallback, and validators trust exactly one address. Rotate
when no hosted operation is in flight.

1. Reviewed change setting `platform_coding_hosted_control_enabled: false`
   (and removing its `REVIEWED_ACTIVATIONS` entry). Converge, then run the
   Deploy Platform workflow for the revision in service. The unit restarts with
   the disabled environment; the identity and Pylon switches stay on.
2. Confirm `hosted_control_configured=false`. From here validator commands fail
   verification, which is the intended fail-closed state.
3. The ceremony replaces the seed at the fixed path, keeping owner `ditto-api`,
   mode and single link. It creates the new file exclusively, renames it into
   place and records the new public address. Custodians retire the old seed and
   its backup under their policy.
4. Reviewed change setting `platform_coding_hosted_control_enabled: true` and
   the new `platform_coding_hosted_signer_hotkey`, restoring the entry.
   Converge. The stat guard checks the new placement.
5. Run the Deploy Platform workflow again, then verify
   `hosted_control_configured=true`, `shadow_only=true` and
   `weight_eligible=false`.
6. Reviewed change to every validator's trust address, then converge each
   validator.

## Revocation

1. Validators remove trust independently of Platform, and first when a
   compromise is suspected: set `validator_stack_coding_hosted_control_enabled:
   false`, clear the address and converge. The validator control command then
   refuses to run, whoever signed the envelope.
2. Set `platform_coding_hosted_control_enabled: false` and converge.
3. Run the Deploy Platform workflow for the revision in service, so the unit
   restarts with the disabled environment. (Because every unit start reads the
   root-owned file, an automatic restart after step 2 is already disabled.)
4. Confirm `hosted_control_configured=false`. The disabled loader no longer
   reads the path.
5. Only then does the ceremony remove the seed from the host and record the
   revocation. Automation never deletes it. A replacement requires a fresh
   ceremony and the enabling order.

## Returning `ditto-api` to pm2

Disable the signer first (Revocation). Then set
`platform_api_service_identity_enabled: false` and converge. `.env` renders
`DITTO_PLATFORM_API_SUPERVISOR=pm2`, and the role stops managing the identity
but removes nothing. The next deploy sees the leftover unit file enabled or
active, runs `ditto-platform-api-release stop` before pm2 starts `ditto-api`,
and continues as before. Removing the users, releases, unit, installer and
sudoers files afterwards is a separate operator step. The Pylon switch can be
reverted the same way; the converge then re-adds `deploy` to `docker`.

## Validator trust

Validators trust only `VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY`. The address never
comes from a command argument, a Platform response, a wallet or a key file,
and it cannot equal the validator's own hotkey. Community validators keep both
values off and empty unless they opt in through their own reviewed
configuration. The validator worker never reads either value.

## Residual risks

These remain after every switch is on.

- **Root.** Anyone with root on the Platform host can read the seed or the
  memory of `ditto-api`. That includes OS Login admins
  (`roles/compute.osAdminLogin` in `infra/terraform/stacks/gcp-platform`) and
  the Deploy Platform workflow's service account, which runs `sudo install` and
  `sudo rm -rf` for relay releases. No seed is in CI, but the CI identity is
  root on the host. So is a local kernel privilege escalation from any user.
- **Signing oracle.** Isolation protects the key, not what `ditto-api` decides
  to sign. `deploy` still runs migrations, relays and the cleanup job, holds the
  database credentials, and supplies the `.env.deploy` values and the dashboard
  bytes. Whoever controls those can influence which operations the API signs
  under its own rules. What `deploy` can no longer do is copy the key, so
  disabling or rotating it ends the exposure.
- **Reviewed code, not the current release.** `install` accepts any revision
  reachable from `main`, including an older one with a known flaw. Anyone who
  can merge to `main`, or a malicious locked dependency, still reaches
  `ditto-api`.
- **Builder cache.** `ditto-api-build`'s uv cache persists between builds. A
  malicious build-time dependency could poison later builds. Clearing
  `/var/lib/ditto-platform-api-build/uv-cache` removes it.
- **GitHub host key.** The installer's `known_hosts` comes from
  `ssh-keyscan` at converge, the same trust-on-first-use the checkout already
  uses.
- **Availability, not confidentiality.** Team members may still start, stop or
  restart the listed units, including `ditto-platform-api.service`. `deploy` may
  stop it through the installer. None of this reads the seed.
- **Docker check scope.** The live check covers group membership, process GIDs
  and the root daemon's socket permissions. A rootless Docker daemon that
  `deploy` started itself could mount only what `deploy` can already read.
- **Operations that changed.** Under the unit, `ditto-api` logs go to the
  journal, not `apps/platform/logs`. `start.sh`, `stop.sh`,
  `scripts/profile-python.sh` and `update.sh` follow the supervisor; the
  `read_platform_logs.sh` skill still reads pm2 files and needs a follow-up
  before step 2.
- **Not rehearsed on a host.** The unit sandbox (for example `skopeo` under
  `RestrictNamespaces`), the sandboxed transient builds and Pylon's container
  recreation are covered only by synthetic tests. The hash-pinned
  `--no-build-isolation` build was rehearsed locally against a read-only copy of
  the source, not on a Platform host. Steps 1 and 2 must be rehearsed on dev.

### Other open decisions

- Only API startup detects a hotkey that does not match the seed.
- Is Secret Manager ruled out as the custodian backup location? Any process on
  the VM can use the VM service account, so it is not a per-user boundary.
- The Ansible ancestor rule forbids group or world write even on root-owned
  sticky directories, which the Python loader accepts. The fixed path's
  ancestors are not sticky, so both agree there.

## Validation

- `uv run pytest -q ditto/tests/test_coding_hosted_control_signer_wiring.py ditto/tests/test_platform_deploy_access.py ditto/tests/test_platform_api_release.py`
- `cd apps/platform && uv run pytest -q -n 0 ditto/tests/api_server/test_coding_hosted_signer_host_wiring.py ditto/tests/api_server/test_coding_hosted_signer_preflight.py ditto/tests/scripts/test_update_script.py`
- From `infra/ansible`:
  `uvx --from ansible-core==2.21.2 ansible-playbook -i localhost, tests/coding-hosted-control-signer.yml`
  renders every env, unit and Pylon template with real Ansible and runs the real
  guards:
  - validator trust cases, including padded own hotkeys;
  - Platform profile cases: screener hotkey, root or `ditto-api` deploy user,
    and each missing isolation switch;
  - the disabled and enabled entry point on the fixed path;
  - the stat guard on synthetic temporary trees: missing, wrong mode, symlink,
    hard link, wrong size, unsafe directory or ancestor, foreign owner, and a
    tree still owned by `deploy`.
