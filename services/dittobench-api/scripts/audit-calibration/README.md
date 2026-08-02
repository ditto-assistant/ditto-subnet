# Transform-audit calibration

Reproduces `docs/BASELINES.md` Run 3: what an honest model scores on the
reproduce-under-transform audit, and whether the metric separates a brittle
harness from an honest one.

This exists because `AUDIT_MIN_ROBUSTNESS` gates an emission-affecting hold. A
floor set above what honest miners actually score quarantines them, so the floor
must come from measurement. The 2026-07-18 run found the metric does **not**
currently separate, which is why enforcement ships off
(`DITTO_TRANSFORM_AUDIT_ENFORCE`).

## Setup

```sh
# 1. locked model + embeddings
ollama serve && ollama pull embeddinggemma
# dittobench-starter-kit/.env: DITTOBENCH_PROVIDER=openrouter,
# DITTOBENCH_MODEL=qwen/qwen3-32b, OPENROUTER_API_KEY=...
cargo build --release --bin dittobench-miner   # in dittobench-starter-kit

# 2. the scoring engine, with the SSRF guard relaxed for a localhost harness
DITTOBENCH_ALLOW_PRIVATE_HARNESS=1 PORT=8000 go run ./cmd/dittobench-api
```

## Honest-model sweep

```sh
SWEEP_SEEDS=$(python3 -c "print(','.join(str(920000+i) for i in range(25)))") \
SWEEP_WORKERS=5 SWEEP_OUT=/tmp/sweep-main.json \
python3 sweep.py
```

Each run gets a **fresh harness process**. That is not incidental: `/seed` is an
idempotent upsert with no clear, so a reused harness stacks several personas'
haystacks into one store, depressing retrieval and contaminating the pairs being
measured. A real scored run faces a fresh container.

Budget ~11 min per run; 25 seeds across 5 workers is about an hour.

## Red-team comparison

`redteam_harness.py` is two non-LLM harnesses that read the seeded haystack
directly (the premise of the attack: the generator is public and the haystack
arrives in cleartext).

- `--mode robust` — a keyword solver, surface-INDEPENDENT.
- `--mode brittle` — the **same solver**, gated on recognising the exact
  question surface against a dump of the generator's base phrasings.

Holding the solver identical between the two is the point: any difference in
`transform_robustness` is caused by surface-keying alone. Both run in seconds
(no model calls).

```sh
SEEDS=$(python3 -c "print(','.join(str(920000+i) for i in range(12)))")
python3 redteam_sweep.py brittle "$SEEDS" 8202
python3 redteam_sweep.py robust  "$SEEDS" 8201
```

`brittle` needs `/tmp/claude-1000/base_surfaces.json`, a dump of base question
surfaces across many seeds (`persona.DeriveQuestions`, excluding
`persona.AuditCaseIDPrefix`). Without it the gate never matches and the harness
looks broken rather than brittle — which is exactly the failure mode of the
first attempt at this measurement, and worth avoiding again.

## Reading the output

Report `transform_robustness`, but also the pair breakdown. The shipped metric
counts a **both-wrong** pair as consistent, so a harness that answers nothing
scores ~1.0. The first brittle run scored 1.000 on every seed purely because it
was answering nothing at all. Always check:

- `both-correct` / `split` / `both-wrong` counts,
- the **conditional** agreement (among pairs with at least one correct half),
  which is what actually tracks brittleness,
- pairs per run against `AUDIT_MIN_PAIRS`.
