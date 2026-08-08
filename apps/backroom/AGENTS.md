## Scope

This is the public SN118 operations console and MCP server hosted at
`backroom.dittobench.ai`. It may call only `ditto-platform` and public subnet
surfaces. Product feature flags, app publishing review, airdrops, and product
ops logs remain in the private `backroom` repository hosted at
`backroom.heyditto.ai`; the subnet half of its MCP now lives here (see
`docs/mcp.md`), and the two tool sets must not be recombined.

Before substantial UI work, run `pnpm dlx @tanstack/intent@latest list` here and
load any matching package skill. Never edit `src/routeTree.gen.ts`; run
`pnpm generate-routes` after route changes.

Validate with `pnpm check`, `pnpm test`, and `pnpm build`. Keep authenticated
responses `no-store`, require same-origin checks for writes, and never expose
`DITTO_ADMIN_API_TOKEN`, the encrypted session, the configured administrator
list, or Google OAuth secrets to browser code. Authentication must remain
independent of Firebase and the private Ditto API.

Every MCP mutation carries the signed-in operator's email to Platform as the
audit actor, so tools must never call the service layer with a shared identity.
Write and artifact access are gated twice: on the OAuth scope and on the
account's live `BACKROOM_ADMIN_EMAILS` level. Keep both.
