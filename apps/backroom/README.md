# Ditto subnet Backroom

The public-source operations console for SN118, hosted at
[`backroom.dittobench.ai`](https://backroom.dittobench.ai). It exposes the
Platform-owned subnet controls: benchmark rollouts, validator and inference
capacity, screener policy/capacity, quarantine and copy review, source-release
policy, submission cadence, score retests, and miner fees.

Ditto product controls are intentionally absent. Feature flags, app publishing
review, production airdrops, product ops logs, and the product MCP stay in the
private Backroom deployment at `backroom.heyditto.ai`.

```bash
pnpm install
pnpm generate-routes
pnpm check
pnpm test
pnpm build
```

Cloudflare Worker secrets:

- `SESSION_SECRET`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `BACKROOM_ADMIN_EMAILS` (comma-separated `@omniaura.ai` write administrators)
- `DITTO_ADMIN_API_TOKEN`

Runtime configuration is in `wrangler.jsonc`. Google OAuth must register
`https://backroom.dittobench.ai/auth/callback`. The existing product Backroom
callback remains registered separately. Any verified `@omniaura.ai` Google
Workspace account receives read access; only `BACKROOM_ADMIN_EMAILS` receives
write access. Sessions expire after 12 hours and roles are recomputed from the
current Worker binding on every request. Workspace-account revocation has an
accepted maximum 12-hour read-only window for an already-issued session; see
`docs/oauth.md` for the immediate-revocation procedure.

This deployment does not use Firebase, `api.heyditto.ai`, or the private Ditto
company-membership endpoint. It only sends operator-attributed requests to the
Platform admin API, whose bearer token remains server-side.
