# LongMemEval judge-substitution study

`gpt-4o-2024-08-06` is the model LongMemEval's official evaluator pins, and it
is the judge frozen into this subnet's v9 confirmation profile
(`longmemeval-official-gpt4o-openrouter-v1`). It retires from the OpenAI API on
**2027-01-14** and from Azure on **2027-04-14**. Every LongMemEval score ever
published, including this subnet's leaderboard, is denominated in a judge that
is going away.

This directory measures what actually happens when you substitute it.

## What is measured

Three things, deliberately separated, because conflating them produces a
confident wrong answer:

1. **Protocol compatibility** — can the candidate even answer under the official
   evaluator's calling convention?
2. **Clause conformance** — does the candidate honour the specific rules written
   into the five official prompt templates?
3. **Agreement and stability** — how often does it match the incumbent, and how
   often does it match *itself* on byte-identical input?

## Provenance

| input | pin |
|---|---|
| judge source | `xiaowu0162/LongMemEval` @ `9e0b455f4ef0e2ab8f2e582289761153549043fc`, `src/evaluation/evaluate_qa.py` |
| dataset | HF `xiaowu0162/longmemeval`, file `longmemeval_oracle`, sha256 `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c` |
| questions | 500 |

The oracle file's question-type histogram (knowledge-update 78, multi-session
133, single-session-assistant 56, single-session-preference 30,
single-session-user 70, temporal-reasoning 133, plus 30 abstention) matches the
2026-07-23 frozen leaderboard runs in `../results/` exactly, so the probes are
built over the same question set those scores were computed on.

`run_judges.py` reimplements `get_anscheck_prompt` verbatim and reproduces the
official call exactly — `temperature=0`, `max_tokens=10`, `n=1`, and label
extraction by `'yes' in response.lower()`. It adds only raw-response capture and
token accounting, which the official script discards.

## The probe suite

`build_probes.py` derives **1,893 probes** from the 500 public questions. Each
probe perturbs a reference answer in a way one of the official templates rules
on *explicitly*, so the gold label comes from the prompt text rather than from
any model's opinion:

| family | n | gold | clause it tests |
|---|---|---|---|
| `P1_exact` | 500 | YES | "answer yes if the response contains the correct answer" |
| `P2_verbose` | 500 | YES | same, with the answer buried in hedging |
| `P3_subset` | 18 | NO | "if the response only contains a subset ... answer no" |
| `P4_offbyone_nontemporal` | 148 | NO | no off-by-one allowance outside temporal-reasoning |
| `P4_offbyone_temporal` | 53 | YES | "do not penalize off-by-one errors for the number of days" |
| `P5_stale_then_updated` | 72 | YES | "previous information along with an updated answer ... correct" |
| `P6_wrong` | 470 | NO | "otherwise, answer no" |
| `P7_updated_to_wrong` | 72 | NO | "as long as the updated answer is the required answer" |
| `P8_explicit_unanswerable` | 30 | YES | "correctly identifies the question as unanswerable" |
| `P9_fabricated` | 30 | NO | fails to identify the question as unanswerable |

`P4_offbyone_temporal` and `P4_offbyone_nontemporal` are the same surface
perturbation with **opposite** gold labels, separated only by question type. A
judge that pattern-matches rather than reading the rubric cannot score well on
both.

Construction is deterministic from the dataset plus `SEED = 20261023`; no model
is involved, so anyone can regenerate a byte-identical corpus.

### Two construction hazards, both found by inspection

Both of these produced plausible-looking but invalid probes on the first pass,
and both are worth knowing before building a suite like this:

- **The dataset's own answers name the off-by-one as acceptable.** Many temporal
  answers read `"30 days. 31 days (including the last day) is also acceptable."`
  Shifting the first number upward lands on a value the reference explicitly
  blesses, so the probe stops testing tolerance. The builder now shifts away
  from any value the reference text already contains, and emits a bare count.
- **Splitting on `", "` manufactures degenerate subsets.** It turned
  `"No, ..."` into the hypothesis `"No"`. `P3_subset` now splits only on `";"`
  or `" and "` and requires both sides to be substantive, which is why n is 18.

### A gold-label defect found by the incumbent, not by inspection

On the first scored run, `gpt-4o` appeared to reject 6% of **verbatim-correct**
answers. All 28 failures were `single-session-preference`. For that type the
dataset's `answer` field is a *rubric*, and the template asks whether the
response "satisfies the desired response" — echoing a rubric is not a
personalized response, so rejecting it is correct judge behaviour and the gold
label was wrong.

`analyze.py` therefore excludes `single-session-preference` from `P1_exact` and
`P2_verbose` scoring. The question type cannot be probed by echo construction;
labelling it would require a human-written correct personalized response per
question, which this suite does not have. Reported totals exclude it rather
than silently averaging a known-bad label in.

## Reproducing

```bash
curl -sSL -o longmemeval_oracle.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_oracle
shasum -a 256 longmemeval_oracle.json   # 821a2034...

python3 -I build_probes.py longmemeval_oracle.json probes.jsonl

export OPENROUTER_API_KEY=...
python3 -I run_judges.py \
  --probes probes.jsonl --refs longmemeval_oracle.json --out full.jsonl \
  --models 'openai/gpt-4o-2024-08-06@official|openai/gpt-4o-2024-08-06|;openai/gpt-6-luna@noreason|openai/gpt-6-luna|{"reasoning":{"enabled":false}}' \
  --reps 3 --workers 40

python3 -I analyze.py full.jsonl
```

`run_judges.py` is resumable: it reads any existing `--out` file and skips
`(arm, probe_id, rep)` triples already present, so an interrupted run costs
nothing to continue.

Arms are `;`-separated (not `,`) because the per-arm override is JSON and
contains commas. The third field is merged into the request body, which is how
the `@noreason` arms set `{"reasoning": {"enabled": false}}`.

## Cost

Judge calls are ~250 prompt tokens and ≤10 completion tokens. The full matrix
(1,893 probes × 9 models × 2 arms × 3 replicates ≈ 102k calls) costs roughly
**$8**. The incumbent is by far the most expensive participant per call.

Note that OpenRouter returned `usage.cost` as `0` for these requests, so the
records carry `completion_tokens` / `reasoning_tokens` and cost is computed from
the published price table rather than reported by the provider.
