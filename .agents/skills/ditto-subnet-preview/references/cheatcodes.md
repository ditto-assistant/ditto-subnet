# Preview-control cheatcodes

In-process Foundry analog (`ditto/preview/engine.py`). Mutations are god-mode:
register without mnemonics, warp blocks, overlay stake/permits. The engine never
talks to finney. Isolation is `assert_isolated`: network in
`{local, localnet, preview, dev}` and a `ws://` / `wss://` loopback endpoint.

`up` stays in the foreground and writes the random bearer token to a mode-0600
worktree state file. A second terminal in **that same worktree** can omit
`--url` / `PREVIEW_CONTROL_TOKEN`. `down` is Ctrl-C in the `up` terminal.

```bash
./scripts/preview up stack --sha "$(git rev-parse HEAD)" --upstream http://127.0.0.1:11434
./scripts/preview state
./scripts/preview urls
./scripts/preview ctl <cheat> [flags]
```

`--upstream` is the healthy relay the fault proxy forwards to when no fault is
injected (localstack-relay is `:11434`).

## Catalog

| Cheat | Foundry analog | CLI | Effect |
|---|---|---|---|
| `register` | `vm.prank` + genesis alloc | `--hotkey <ss58> [--permit] [--stake N]` | Overlay neuron, no mnemonic |
| `permit` | — | `--hotkey <ss58>` / `--clear` | Set or clear validator permit |
| `warp_block` | `vm.roll` | `--n N` | Advance overlay head; expires due leases |
| `warp_tempo` | — | `--n N` | `warp_block(n * tempo)` (default tempo 360) |
| `issue_lease` | — | `--hotkey <ss58>` | Preview validator lease |
| `expire_lease` | — | `[--lease-id ID]` | Expire one lease, or every live lease |
| `issue_grant` | — | (none) | Preview inference grant |
| `exhaust_allowance` | — | `[--grant-id ID]` | Mark grant(s) `inference_allowance_exhausted` |
| `inject_provider` | — | `--status 429\|503` / `--clear` | Fault proxy returns that status |
| `drop_relay` | — | / `--clear` | Fault proxy refuses relay connections |
| `snapshot` | `vm.snapshot` | `--name NAME` | Named overlay checkpoint |
| `revert` | `vm.revert` | `--name NAME` | Restore a named checkpoint |
| `align_from_db` | fork alignment | `[--json-path PATH] [--database-url URL]` | Register hotkeys from JSON or preview Postgres |

`inject_provider` accepts only `429`, `503`, or clear. Hotkeys must be SS58
(`^[1-9A-HJ-NP-Za-km-z]{46,50}$`). Example hotkey used in tests:
`5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY`.

## Fault proxy + localstack

When `FAULT_PROXY_URL` is set, `localstack/lib.sh` points
`HARNESS_GATEWAY_URL` and `HARNESS_EMBED_URL` at the proxy so
`inject_provider` / `drop_relay` crank the same inference path the scorer uses.

```bash
# terminal 1
uv run python -m ditto.preview up stack --sha "$(git rev-parse HEAD)" --upstream http://127.0.0.1:11434
# terminal 2
FAULT_PROXY_URL=<printed-fault-proxy-url> make localstack-up
./scripts/preview ctl inject_provider --status 429
```

## Snapshot restore

`scripts/preview-restore-snapshot.sh` runs destructive `pg_restore --clean` and
permits only the checked-in loopback identity in `preview/compose.yml`
(`postgres://ditto:preview@127.0.0.1:5433/ditto_platform_preview`). Other hosts,
ports, users, and database names fail closed. Restore does not launch Platform
or a validator. After restore, `align_from_db` so the overlay matches Postgres.

```bash
docker compose -f preview/compose.yml up -d
PREVIEW_DATABASE_URL=postgres://ditto:preview@127.0.0.1:5433/ditto_platform_preview \
  ./scripts/preview-restore-snapshot.sh /path/to/sanitized.dump
./scripts/preview ctl align_from_db --json-path preview/fixtures/hotkeys.json
```

## Tests

```bash
uv run pytest ditto/tests/preview -q
```

`test_engine.py` covers isolation, register/permit/warp/snapshot, lease expiry,
allowance, provider faults, and JSON alignment. `test_server.py` covers the HTTP
cheatcode round-trip. `test_workflow.py` asserts the GitHub job exists.
