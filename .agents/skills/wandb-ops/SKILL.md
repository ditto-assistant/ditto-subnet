---
name: wandb-ops
description: Inspect Weights & Biases projects, runs, histories, summaries, and table artifacts for live operational diagnosis. Use when comparing validator or worker runs, tracing a failure across nodes, checking whether an error is shared or isolated, correlating timestamps with Platform or chain state, or summarizing W&B evidence without exposing credentials.
---

# W&B Operations

Use the authenticated W&B API first. It is faster and more complete than browser
inspection for structured run data. Use the browser only for a view the API does
not expose.

## Query

Run [`scripts/query_wandb.py`](scripts/query_wandb.py) with the smallest command
that answers the question:

```bash
uv run --with wandb python .agents/skills/wandb-ops/scripts/query_wandb.py runs
uv run --with wandb python .agents/skills/wandb-ops/scripts/query_wandb.py \
  summary --run RUN_ID --prefix weights/ --prefix ledger/
uv run --with wandb python .agents/skills/wandb-ops/scripts/query_wandb.py \
  history --run RUN_ID --key weights/status --key weights/miner_count --tail 20
uv run --with wandb python .agents/skills/wandb-ops/scripts/query_wandb.py \
  table --run RUN_ID --name weights
```

The defaults are `heyditto/ditto-sn118`. Override `--entity` and `--project`
when the task names another project. Output is JSON so it can be narrowed with
`jq` without scraping terminal tables.

If `WANDB_API_KEY` is configured only by an interactive shell, invoke the
command through that shell. Check only whether the variable is present; never
print, inspect, or copy its value:

```bash
zsh -ic 'exec uv run --with wandb python \
  .agents/skills/wandb-ops/scripts/query_wandb.py runs'
```

## Diagnose

1. Establish the project, time window, run ID, validator or job ID, and node.
2. Start with `runs`; duplicate display names can be restarts, so compare run IDs.
3. Read only relevant summary prefixes, then fetch a bounded history tail. Avoid
   config reads and unbounded file or history listings.
4. Inspect a named table only when scalar telemetry is insufficient.
5. Compare at least one successful or progressing peer before calling a failure
   fleet-wide.
6. Corroborate W&B with the authoritative Platform API, production read-only DB,
   deployed release identity, or chain state as appropriate.

Separate scoring failures from telemetry-only failures, host pressure or process
loss, stale logs, and accepted-but-not-yet-visible chain writes. Report exact run
IDs, timestamps, affected nodes, counterexamples, and the smallest next check.

## Guardrails

- Never print, retrieve, log, commit, or transmit `WANDB_API_KEY` or other secrets.
- Never dump a run's full config or environment. Query named summary/history keys.
- Treat W&B as potentially partial; absence from a bounded history is not proof.
- Do not equate high CPU, a heartbeat warning, or `pylon_accepted` with scoring
  failure or revealed on-chain weights.
- Keep W&B work read-only unless the user explicitly requests a mutation.

For SN118, correlate validator hotkeys and agent IDs with:

- `https://platform-api.heyditto.ai/api/v1/public/validators`
- `https://platform-api.heyditto.ai/api/v1/public/agent/<agent_id>/pipeline`
- `https://platform-api.heyditto.ai/api/v1/public/weights`

Platform owns tickets and accepted scores; the chain owns revealed weights; W&B
is diagnostic telemetry.
