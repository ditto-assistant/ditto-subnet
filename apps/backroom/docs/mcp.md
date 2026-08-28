# SN118 Backroom MCP

`https://backroom.dittobench.ai/mcp` is an OAuth-protected Streamable HTTP MCP
server exposing the same operations as the console: screening quarantines and
disputes, validator queue/slot/inference policy, benchmark rollouts, scoring
policy, scores and leaderboards, and the emission burn.

It was ported from the private `ditto-assistant/backroom` repository, which
keeps only `backroom.heyditto.ai` and the Ditto app surface. Feature flags and
app reviews are deliberately **not** served here: they reach the private product
API this deployment holds no credentials for. `src/lib/subnet-surface.test.ts`
enforces both halves of that boundary — the MCP must be wired, those tools must
not be.

## Authorization

The MCP endpoint is a full OAuth 2.1 resource. `@cloudflare/workers-oauth-provider`
owns `/authorize`, `/token`, and `/register`; discovery lives at
`/.well-known/oauth-authorization-server/mcp` and `/.well-known/mcp/server.json`.
A client registers dynamically, then the operator approves the connection on
`/oauth/consent` while signed in to Backroom with Google.

Three scopes, in ascending sensitivity:

| Scope | Grants |
|---|---|
| `backroom:read` | Every read. Required for any connection. |
| `backroom:artifact:read` | Miner-submitted source: tarball URLs, file listings, source search, copy and baseline diffs. |
| `backroom:write` | Mutations, including `set_burn_settings`, which moves TAO. |

Two independent gates apply to every privileged call. The grant must carry the
scope, **and** the operator's account must still resolve to `write` through
`BACKROOM_ADMIN_EMAILS`. The level is re-derived from that binding at consent
time rather than read from the session cookie, so removing an address stops the
next authorization from minting a privileged grant even while a 7-day session
is still live. `mcp-scope.server.ts` additionally challenges the request with a
`WWW-Authenticate` scope hint before the tool runs, so an under-scoped client
gets a 403 naming the scope it needs rather than a tool-level refusal.

Access tokens never outlive the operator session: `tokenExchangeCallback`
refuses an expired one and clamps the token TTL to the session's remaining life.
There is no refresh path for the identity itself — when the session ends, the
operator authorizes again.

## Bindings

Beyond the console's own secrets (`docs/oauth.md`), the MCP needs:

- `OAUTH_KV` — registered clients, grants, and issued tokens. Terraform owns the
  namespace (`infra/terraform/stacks/cloudflare-dittobench`) and exports
  `backroom_oauth_kv_namespace_id`. That value belongs in the `prod` environment
  as the `BACKROOM_OAUTH_KV_ID` variable, **not** in `wrangler.jsonc`: this
  repository is public and the namespace holds live operator grants and tokens.
  `scripts/inject-oauth-kv.mjs` binds it immediately before `wrangler deploy`,
  and the committed placeholder is not a valid id, so a deploy that skips that
  step fails at Cloudflare rather than shipping a Worker that cannot persist a
  grant. `subnet-surface.test.ts` fails if a real id is ever committed.
  Deleting the namespace revokes every operator's MCP connection.
- The hourly cron trigger, which purges expired grants and tokens.

## Adding a tool

Tools wrap `admin.service.ts`, the same layer the console's server functions
call, so a new capability is one `registerTool` entry:

1. Add or reuse the service function and its zod schema in `admin.schemas.ts`.
2. Register the tool in `mcp.server.ts`. Writes go through `write(() => …)` and
   pass `props.session.email` so the platform records the real operator as the
   audit actor; artifact reads go through `artifact(() => …)`.
3. Add the name to `WRITE_TOOL_NAMES` (or `TOOL_SCOPE_REQUIREMENTS` for artifact
   scope) so the pre-flight scope challenge covers it.
4. Add it to the expected catalog list in `mcp.server.test.ts`. That list is
   exhaustive on purpose: a tool that is not named there fails the suite.

Keep the catalog description in `MCP_CATALOG_DESCRIPTIONS` short — the whole
catalog is loaded into model context before any call, and the test bounds both
the total and the per-description length. Long-form operational notes belong in
the `description` field, which `get_backroom_tool_help` serves on demand.

## Paging

A paged tool answers with `count` (the upstream total), `returned` (rows in this
response), `limit`, `offset`, and `has_more`. `has_more` is the only field that
reports MCP paging. An upstream `truncated` flag is a different fact: the
platform stopped short of a complete answer — paths it dropped before paging, or
a scan that hit its own match cap — so no later offset recovers what it covers,
and any total it accompanies is a lower bound. The two are not interchangeable,
and neither one substitutes for the other.

Where the window is resolved differs by tool. `search_screening_source` pages on
the platform, which holds the whole ordered match list, so the Worker must not
re-slice a window it never had. A collection the platform returns whole is paged
here by `paginateLocalCollection`, which is what emits `returned` and `has_more`.

Default page sizes are bounded so one call cannot flood model context, but a
manifest an operator reads to decide *what exists* — today
`list_screening_source_files` — defaults to the platform's whole listing rather
than a page, because a row silently missing from page one is evidence the
reviewer never learns to ask for. `mcp.server.test.ts` pins each tool's bound.

## The review queue

`get_screening_review_queue` is the operator queue: unresolved `ath_reviews`
rows, oldest hold first. It is **not** `list_screening_quarantines` — active
quarantines are auto-resolved by the platform within milliseconds and that list
is effectively always empty, which is what made the queue look empty when this
tool pointed at it.

Two platform parameters are deliberately not exposed. `status`, because a queue
is unresolved work by definition. `generation`, because it filters on whether
the held agent has a score at a benchmark version — its `active` default hides
an upload-time copy hold and any hold that outlived a rollout, both of which are
still waiting. Both are pinned in `admin.service.ts`.

Read `agent_status` before acting on a row. A `pending` review whose agent is
not in `ath_pending_review` is a hold stranded by some other path, and
`resolve_ath_review` answers 409 for it; `apps/platform/docs/ath-review-queue.md`
lists the paths that produce it.

## Hotkey-level upload bans

An ATH rejection bans one agent UUID. The separate `banned_hotkeys` gate is a
rare miner-wide control that refuses every future upload from one hotkey. Use
`list_hotkey_bans` to enumerate the active rows and retain the exact `hotkey`,
stored `reason`, and `banned_at` timestamp.

`unban_hotkey` requires that timestamp as `expectedBannedAt`, a specific
operator reason, and the exact confirmation `UNBAN HOTKEY <hotkey>`. Platform
locks and rechecks the active row, removes only that upload gate, and appends
the signed-in operator identity plus the previous ban evidence to
`hotkey_ban_audit`. Existing agent UUID statuses are never changed. The tool
returns the post-write state so a successful call proves `banned=false` and
surfaces the new audit entry.

## Reading miner source

Three tools, used in this order. Skipping the middle one is the expensive
mistake:

1. `list_screening_source_files` — the manifest: which paths exist and which
   are opaque blobs the text reader cannot show.
2. `search_screening_source` — **where**. One regex or literal scan of the whole
   artifact returning `{path, line, text}`. A miner `baseline.rs` routinely runs
   past 10,000 lines, so without this, locating the `protocol::RunResponse`
   construction that a `deferred_source_review` turns on means bisecting with
   400-line windows: six to eight reads per agent, repeatedly.
3. `read_screening_source_file` — the excerpt, once you have a line number.

Both reads page under the rules above: the search `truncated` is the scan
hitting its own match cap, and the manifest returns whole by default so no path
hides behind an offset. `opaque_skipped` counts the members no search can
reach — a `.onnx` or `.bin` weights file is never searched, and a search that
never opened one cannot clear it.
