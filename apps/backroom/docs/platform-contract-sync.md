# Platform contract synchronization

Platform and Backroom now share this repository. Regenerate the OpenAPI client
directly from `apps/platform`; no repository dispatch, bot branch, or cross-repo
version-bump PR is involved.

From `apps/backroom`:

```bash
scripts/platform-contract/generate.sh
pnpm check
pnpm test
pnpm build
```

The generator records the monorepo commit and `apps/platform` path next to the
generated types. A Platform API change and its Backroom consumer can therefore
land in one stack with one CI result.
