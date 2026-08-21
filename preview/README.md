# SN118 preview channels

Same profiles and cheatcodes locally and in GitHub Actions. Previews never
publish GitHub Releases, `v*` images, or `compat-2`.

## Profiles (multi-select)

| Profiles | What runs | Talks to |
|---|---|---|
| `dashboard`, `backroom`, or both | SPA / Worker preview | **Production Platform** |
| `stack` | Platform + one **localnet** validator + scorer | Isolated only |
| `stack-copy` | `stack` plus a restored snapshot, then `align_from_db` | Isolated only |

Illegal: preview Platform attached to production validators.

```bash
./scripts/preview compose dashboard,backroom
./scripts/preview compose stack
./scripts/preview compose stack --attach-prod-api   # exits 2
```

## Local

```bash
# Frontends against prod API (no validator).
./scripts/preview compose dashboard,backroom

# Isolated stack: overlay engine + optional Postgres + fault proxy.
uv run python -m ditto.preview up stack --sha "$(git rev-parse HEAD)"
# Ctrl-C stops it.

# Cheatcodes (Foundry analog) against the running control URL:
export PREVIEW_CONTROL_URL=http://127.0.0.1:…..  # printed by `up`
./scripts/preview ctl register --hotkey 5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY --permit
./scripts/preview ctl warp_block --n 20
./scripts/preview ctl inject_provider --status 429
./scripts/preview ctl inject_provider --clear
./scripts/preview ctl align_from_db --json-path preview/fixtures/hotkeys.json
```

`stack-copy` after Postgres is up:

```bash
docker compose -f preview/compose.yml up -d
PREVIEW_DATABASE_URL=postgres://ditto:preview@127.0.0.1:5433/ditto_platform_preview \
  ./scripts/preview-restore-snapshot.sh /path/to/sanitized.dump
```

Point localstack's relay at the fault proxy so injected 429s hit inference:

```bash
FAULT_PROXY_URL=http://127.0.0.1:….. HARNESS_GATEWAY_URL=$FAULT_PROXY_URL make localstack-up
```

## Cheatcodes

| Command | Effect |
|---|---|
| `register` / `permit` | God-register a hotkey on the overlay (no mnemonic) |
| `warp_block` / `warp_tempo` | Advance overlay time; leases expire when due |
| `issue_lease` / `expire_lease` | Preview leases |
| `issue_grant` / `exhaust_allowance` | `inference_allowance_exhausted` |
| `inject_provider` | Fault proxy returns 429 or 503 |
| `drop_relay` | Fault proxy returns 502 |
| `align_from_db` | Register every hotkey from a snapshot JSON or Postgres |
| `snapshot` / `revert` | Named overlay checkpoints |

The engine refuses `finney` and OpenTensor public endpoints.

## GitHub

`.github/workflows/preview.yml` accepts comma-separated `profiles` and an
optional SHA. It always proves composition + cheatcodes. Cloud publish of
dashboard/backroom URLs needs the `preview` environment secrets and a
Terraform-applied `*.preview.dittobench.ai` wildcard.
