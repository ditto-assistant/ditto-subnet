# Repository guidance

## Monorepo context

This repository owns the complete public SN118 system: miner and validator code
at the root, Platform and Backroom under `apps/`, services and workers under
`services/` and `workers/`, shared contracts under `packages/`, benchmark
research under `research/`, and cloud state under `infra/`.

Start substantial work with:

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py "<task>"
```

Use `.agents/skills/ditto-subnet-worktree` for isolation. Do not use
multi-repository temp clones or cross-repository synchronization for components
already present in this tree. Read the nearest nested `AGENTS.md` or `CLAUDE.md`
before editing a component.

## Preview control harness

From a worktree, resolve a preview plan and run the loopback-only mock control
and fault proxy with the same checks GitHub Actions uses. `up` does not launch
Platform, Backroom, a chain, scorer, or validator; those are requirements in a
`stack` plan, not implemented services. Only the public dashboard may target
production Platform. Backroom always requires an isolated stack.

```bash
./scripts/preview compose dashboard
uv run python -m ditto.preview up stack --sha "$(git rev-parse HEAD)"
```

See `$ditto-subnet-preview` and `preview/README.md`.

## Backroom MCP debug first

The public Backroom MCP at `https://backroom.dittobench.ai/mcp` is the only
production control plane. When a live diagnosis is missing a knob, a queue
reason, a budget, a last-applied setting, or a review audit, add a
`backroom:read` tool that returns it. Do not infer production state from the
dashboard, logs, or a local default. Err toward more Backroom visibility, not
less: a setting that exists only in the operator UI is not operator-visible
to agents.

## Agent and Claude guidance

`AGENTS.md` is the repository-wide instruction file for general agents.
`CLAUDE.md` is Claude-specific: it can name Claude abilities, slash commands,
and Claude-only routing. Keep shared policy aligned. Do not treat the files as
copies of each other.

## Skills for `.agents` and `.claude`

Every repository skill must be available under both `.agents/skills/<name>`
and `.claude/skills/<name>`.

- When the skill is the same for every agent, put the canonical tree in
  `.agents/skills/<name>` and symlink `.claude/skills/<name>` to
  `../../.agents/skills/<name>`.
- When Claude needs different frontmatter, allowed-tools, or slash-command
  behavior, keep a real directory at `.claude/skills/<name>` (see
  `.claude/skills/impeccable`). Agent-only extras can live under
  `.agents/skills/<name>/agents/` without forcing the Claude tree to match
  file-for-file.
- A new shared skill is incomplete until the `.claude/skills` path exists.
- Component-local trees such as `apps/platform/.agents/skills/<name>` are not
  the canonical home; they must symlink back to `.agents/skills/<name>`.

## Semantic-release commit types

- Do not use `test:` as a commit or pull-request title prefix; it is not a valid
  semantic-release type in this repository.
- Test-only changes must use the `chore(tests):` scope, for example
  `chore(tests): pin legacy confirmation regression`.

## Forward-compatible JSON models

- Pydantic wire models must ignore unknown JSON fields so rolling upgrades can
  add fields without breaking older consumers. Use `ConfigDict(extra="ignore")`
  when the policy should be explicit; never use `extra="forbid"`.
- Keep known-field validation strict through field types, bounds, validators,
  required fields, and signature verification. Ignoring an unknown field must
  not make that field authoritative or include it in canonical signed output.

## Git-backed Docker build contexts

- Never pin a Git build context with the legacy URL-fragment form
  `https://github.com/OWNER/REPO.git#<commit-sha>`. A clean Docker builder may
  fail because the commit SHA is not an advertised branch or tag.
- For a commit on `main`, use BuildKit's structured query syntax with both an
  advertised ref and the full 40-character checksum:
  `https://github.com/OWNER/REPO.git?ref=refs/heads/main&checksum=<commit-sha>`.
  The ref makes the repository fetchable, and the checksum makes the build
  fail closed if the ref does not resolve to the expected commit.
- Apply the same rule to other branches or tags: use their fully qualified ref
  plus a full checksum. Do not replace an immutable checksum with a floating
  ref alone.
- Verify every changed remote Git context with a fresh or empty BuildKit
  builder. A successful build against a warm local cache is not sufficient.
