## Scope

This is the public SN118 operations console hosted at
`backroom.dittobench.ai`. It may call only `ditto-platform` and public subnet
surfaces. Product feature flags, app publishing review, airdrops, product ops
logs, and the product Backroom MCP remain in the private `backroom` repository
hosted at `backroom.heyditto.ai`.

Before substantial UI work, run `pnpm dlx @tanstack/intent@latest list` here and
load any matching package skill. Never edit `src/routeTree.gen.ts`; run
`pnpm generate-routes` after route changes.

Validate with `pnpm check`, `pnpm test`, and `pnpm build`. Keep authenticated
responses `no-store`, require same-origin checks for writes, and never expose
`DITTO_ADMIN_API_TOKEN`, the encrypted session, the configured administrator
list, or Google OAuth secrets to browser code. Authentication must remain
independent of Firebase and the private Ditto API.
