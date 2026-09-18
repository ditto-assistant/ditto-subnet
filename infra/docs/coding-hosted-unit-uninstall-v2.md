# Native worker unit and custody template uninstall

The `coding_hosted_unit_uninstall` role and
`infra/ansible/playbooks/gcp-coding-hosted-unit-uninstall.yml` remove the two
systemd unit files the native Coding host's install roles create on
`ditto-coding-hosted-v2`, then run `systemctl daemon-reload`. The role is off by
default. It stops nothing and starts nothing. It needs no secret.

## What it removes

| Path | Installed by |
|---|---|
| `/etc/systemd/system/ditto-coding-hosted-worker.service` | `coding_hosted_connectivity` |
| `/etc/systemd/system/ditto-coding-custody@.service` | `coding_hosted_custody_service` |

Neither install role adds a drop-in, an instance file or an `[Install]` section,
so it never creates an enablement link. The role removes nothing else.
`ditto/tests/test_coding_hosted_unit_uninstall.py` parses the install roles'
tasks and fails on any of these:

- a change to the template destination, owner or mode;
- a template that gains `[Install]`;
- in an install role, a `file` link, a `systemd`/`service` argument other than
  `daemon_reload`, or a command that enables, links, masks or presets;
- a mention of either unit by any other task, except the read-only
  `is-active`/`list-units` queries;
- any other role, handler or playbook task that names either unit, after
  resolving `{{ var }}` from defaults, vars, group_vars and host_vars and
  `{{ item }}` from literal loops.

A variable-built `/etc/systemd/` path that cannot be resolved and could name
either unit must be on an explicit allowlist. Today that is only
`screener_partition`'s drop-ins, and nothing that defines their loop names a
native Coding unit.

## What it never touches

- Credentials. The PostgreSQL environment copies belong to the cleanup role in
  #1897, and the worker credential files belong to the cleanup role in #1925.
- Data directories under `/var/lib/ditto-coding-hosted` and
  `/var/lib/ditto-coding-custody`, including release inputs, the RSA key and
  per-run configuration.
- Accounts, Docker, the rootless daemon, the egress guard and the runtime
  install.
- The running state of any unit.

## Run it

First stop the hosted worker and every custody instance. Each unit must be
inactive or failed, with no queued job. Then run:

```bash
GCP_OSLOGIN_USER=… ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-coding-hosted-unit-uninstall.yml \
  --limit ditto-coding-hosted-v2 \
  -e '{"coding_hosted_unit_uninstall_enabled": true,
       "coding_hosted_unit_uninstall_confirmation": "UNINSTALL NATIVE CODING WORKER AND CUSTODY UNITS",
       "coding_hosted_unit_uninstall_source_revision": "<40-char reviewed commit>"}'
```

Pass the inputs as JSON. The `key=value` form makes the flag the string
`"true"`, and the role treats that as disabled.

## Guards, in order

1. **Preset names (`main.yml`).** Before the role creates any name, it refuses
   every `coding_hosted_unit_uninstall_*` variable except the three inputs. That
   covers a `-e` preset of the gate, a captured input or a registered result.
2. **Gate freeze.** The enabled flag is frozen once, under `no_log`, as
   `(enabled | default(false, true)) is sameas true`. The `bool` filter is never
   used: on ansible-core 2.21.2 it prints its input in a deprecation warning.
3. **Dynamic include.** The removal lives in `remove.yml`, reached through
   `include_tasks`. `--start-at-task` cannot jump into it.
4. **Include preset refusal.** The first task in `remove.yml` repeats the preset
   refusal, with only the gate added to the allowed names. It also requires the
   raw enabled flag to be boolean true. So neither a `--start-at-task` past
   `main.yml` nor a preset gate can open the removal.
5. **One host.** The play must target exactly one host. Both
   `ansible_play_batch == ['ditto-coding-hosted-v2']` and
   `ansible_play_hosts_all == ['ditto-coding-hosted-v2']` must hold, so
   `serial: 1` without `--limit` is refused.
6. **Check mode.** An enabled run in check mode is refused. The disabled fixture
   covers `--check`.
7. **Confirmation and revision.** Both are frozen once under `no_log` and must
   match the exact confirmation and a 40-character lowercase-hex revision.
8. **Host identity.** The host must be Debian 13 x86_64 `ditto-coding-hosted-v2`.
   Identity comes from a registered `setup` probe. The playbook sets
   `gather_facts: false` because gathered facts could be forged with `-e`.
9. **Live units and queued jobs (refuse first).** Every `systemctl list-units`
   line for the worker or a `ditto-coding-custody@*` instance must show the unit
   inactive or failed. An empty listing passes. Anything else is refused,
   including `active`, `activating`, `deactivating`, `reloading`, `refreshing`,
   `maintenance`, an unknown state or an unparseable line. JOB is an optional
   `list-units` column, so a line such as `loaded inactive dead start` cannot be
   told apart from a description. `systemctl list-jobs` for both units must
   therefore print nothing.
10. **Pinned unlink and in-call reload.** The role-local
    `coding_hosted_unit_uninstall_unlink` module opens `/etc/systemd/system`
    from `/` one component at a time with `O_NOFOLLOW`. The
    `etc/systemd/system` directories must be root-owned and not writable by
    group or others.
    - It first scans the unit directory for anything neither install role
      creates: a drop-in, an instance file, or a link in a `.wants`,
      `.requires` or `.upholds` directory. If it finds one, it refuses and
      removes nothing.
    - It removes the two names with `unlinkat`. It refuses a symlink, directory,
      special file, hard link or non-root file, and attempts nothing after the
      first refusal.
    - It records each outcome as it happens, so the receipt stays true on every
      path. That includes an OS error on the second unlink, a failed `fsync`,
      and a name replaced right after its unlink (counted as removed and
      refused).
    - It then runs `systemctl daemon-reload` in the same invocation, including
      after a partial removal or a refusal. The window in which a start could
      land between a removed file and systemd dropping its definition is
      therefore one module call, not an extra SSH round trip.
    - A failed reload is refused and reported as `daemon_reloaded=false`.
11. **Re-check.** The units must still be inactive or failed, and `list-jobs`
    must still print nothing. This check runs before the receipt is enforced,
    so an orphaned unit is always reported; see below.
12. **Receipt.** The run fails unless the module refused nothing, both paths
    were removed or already absent, and the reload ran. The failure lists
    `refused`, `foreign`, `removed`, `already_absent`, `not_attempted` and
    `daemon_reloaded`.
13. **Positive absence.** `systemctl show --property=LoadState --value` must
    return rc 0 and exactly `not-found` for both
    `ditto-coding-hosted-worker.service` and
    `ditto-coding-custody@00000000-0000-0000-0000-000000000000.service`.
    - A template cannot be shown directly. The all-zero instance, which
      `custody-run.py` never issues, loads only if a custody template exists
      anywhere systemd looks.
    - Empty output does not pass, and neither does a systemctl or D-Bus error.

## If a unit went live during the uninstall

A start can land after the live check and before the reload. The unit then
keeps running as `not-found`. Its file is gone, so systemd runs no
`ExecStopPost` cleanup and no `RuntimeMaxSec` applies. The role refuses on its
re-check and names both steps. Do both by hand, in order:

```bash
# Worker
sudo systemctl stop ditto-coding-hosted-worker.service
sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-hosted/connectivity-policy.py revoke

# Each live custody instance
sudo systemctl stop ditto-coding-custody@<worker-uuid>.service
sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-custody/custody-run.py release <worker-uuid>
```

The first command pair stops the worker and revokes its connectivity grant. The
second stops a custody instance and releases its per-run configuration. Then
rerun the uninstall. It reports the already-removed paths as `already_absent`.

Every failure message is constant, apart from the module's fixed-path lists. The
report shows only the source revision and the `removed` and `already_absent`
lists.

A second run is idempotent. It reports both paths as `already_absent` and
reloads again.

## Residuals

- Files the install roles create outside the unit directory stay on the host:
  - `custody-run.py`
  - `/etc/ditto-coding-custody/base.json`
  - the worker's unwrap proxy
  - release inputs
  - `connectivity-policy.py`
  - `/etc/ditto-coding-hosted/connectivity.json`

  Without their units these files are inert. They stay on purpose so a
  reinstall can reuse them. Removing them needs a separate, narrowly reviewed
  retirement role; this uninstall will not be broadened to cover them.
- Failed instances stay in `list-units` as `not-found failed`, which keeps the
  failure evidence. The role never runs `systemctl reset-failed` and runs no
  state-changing systemctl command. Once the retained failures have been
  reviewed, the operator may run `systemctl reset-failed` for the two units by
  hand as a separate, explicit step.
- The module scans only `/etc/systemd/system`. It does not scan
  `/etc/systemd/system.control`, `/run/systemd` or `/usr/lib/systemd`. The
  post-reload `LoadState` check catches a unit file there, but not a drop-in
  alone.
- A start can still land in the one module call between the unlinks and the
  in-call reload. The re-check reports it, and the operator runs the manual
  cleanup above.
- Reinstalling means running `coding_hosted_connectivity` and
  `coding_hosted_custody_service` again with their own reviewed inputs.

## Rehearsal

Setting `DITTO_ANSIBLE_REHEARSAL=1` enables the rehearsal:

```bash
DITTO_ANSIBLE_REHEARSAL=1 uv run --locked --only-group dev \
  pytest -p no:cacheprovider -rs ditto/tests/test_coding_hosted_unit_uninstall.py
```

It runs the real role through ansible-core 2.21.2 against temporary unit trees. The Ansible
job in `infra-ci.yml` runs the same command. The rehearsal covers:

- a disabled or non-boolean flag doing nothing;
- a wrong confirmation or revision;
- every live-unit state, and a queued job before or during removal;
- every preset internal name;
- `--start-at-task` at every task;
- symlink, hard link, directory, parent-symlink, drop-in and enablement-link
  swaps;
- an extra host and a `serial: 1` play;
- forged facts;
- partial removal reporting;
- a failed in-call reload;
- a shadow or empty `LoadState` answer;
- an idempotent second run.

It then removes or weakens each guard in turn and shows that the unsafe outcome
follows.
