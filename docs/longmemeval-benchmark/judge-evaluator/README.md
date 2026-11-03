# The LongMemEval judge evaluator SN118 runs

LongMemEval's official judge, `gpt-4o-2024-08-06`, is hard-coded in the
upstream evaluator and **retires on 2027-01-14 (OpenAI API) and 2027-04-14
(Azure)**. SN118 scores submissions with `deepseek/deepseek-v4.1-flash`, so the
miner rehearsal has to judge with that too — otherwise miners tune against a
grader that does not pay them.

Substituting the judge needs a forked evaluator, not an argument. This
directory records the fork so the delta is reviewed here, in the repository
that depends on it, rather than only in the fork.

| | |
|---|---|
| upstream | [`xiaowu0162/LongMemEval`](https://github.com/xiaowu0162/LongMemEval) @ `9e0b455f4ef0e2ab8f2e582289761153549043fc` (MIT, © 2024 Di Wu) |
| fork | [`ditto-assistant/LongMemEval`](https://github.com/ditto-assistant/LongMemEval) @ `refs/heads/ditto-judge-v1` |
| upstream `evaluate_qa.py` | `ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251` |
| fork `evaluate_qa.py` | `d15f87dee2c627a7c9ab2a63b34eae974cc349b964ad38b6c90e3b76dc363609` |

- `upstream/evaluate_qa.py` — the pinned original, byte-exact
- `upstream/LICENSE` — upstream's MIT licence, as required for redistribution
- `ditto/evaluate_qa.py` — the forked evaluator, byte-exact
- `ditto-judge-v1.patch` — the delta between them, for humans

Both files are vendored so every check runs offline, with nothing to skip and
no network dependency that could turn a failure into a pass.

## Why it is not a model-name change

The official protocol assumes a **non-reasoning** judge. Upstream pins
`max_tokens=10`, which is correct for `gpt-4o` — it answers the evaluator
prompt in one or two tokens. A reasoning judge spends that entire budget on
reasoning tokens, emits no visible content, and returns `content=""` with
`finish_reason="length"`. Upstream then evaluates:

```python
label = 'yes' in eval_response.lower()   # 'yes' in '' -> False
```

…and scores **every question "no"**. The run completes, reports total failure,
and raises nothing. Measured empty-response rates under the official
convention: `qwen3.8-flash` 100%, `mistral-large-4-0` 99.3%,
`deepseek-v4.1-flash` 92%, `kimi-k3` 75.9%, `glm-5.3-flash` 72.4%,
`gpt-6-luna` 23.9%.

`reasoning: {"enabled": false}` drops all of them to 0.0%, but it is not a
general fix: `glm-5.3-flash` and `minimax-m3.1-flash` rejected that flag on
5,679 of 5,679 calls — *"Reasoning is mandatory for this endpoint and cannot be
disabled."* So the bound is raised instead.

## The delta — three hunks

1. **`model_zoo` gains `deepseek-v4.1-flash`.** Upstream's three entries are
   untouched, which `test_the_judge_is_registered_with_the_production_model_id`
   checks entry by entry.
2. **`max_tokens` becomes per-judge** via `max_tokens_zoo`, defaulting to
   upstream's 10. `gpt-4o` through the fork is therefore byte-equivalent to
   upstream. 8192 comes from measurement: over the 76 probes that truncated
   `deepseek-v4.1-flash` at a 1024 bound, re-run at 8192 × 3 replicates, the
   observed completion maximum was 2,907 tokens with zero truncations. The
   bound is a cap, not a charge — mean observed completion is 62 tokens.
3. **An empty completion raises** instead of being labelled "no", matching
   `provider_runtime.go`, which refuses the same response with *"official
   LongMemEval judge response is malformed"*.

## What is checked, not asserted

`services/dittobench-api/integrations/longmemeval/test_judge_evaluator.py`
runs offline against both vendored files:

- both digests equal the values declared in `judge_evaluator.py`
- `get_anscheck_prompt` and all five prompt templates are **byte-identical** —
  the property that makes a substituted run comparable at all
- the temporal rubric's off-by-one clause survives verbatim
- the fork sends `temperature=0`, `n=1` and the frozen bound, and names its
  output `.eval-results-deepseek-v4.1-flash` after the judge that ran
- the fork **raises** on a truncated verdict, and upstream **silently labels it
  `False`** — the second half pins the bug the fork exists to fix, so if
  upstream ever changes, the comparison in every report built on this stops
  being stale silently

Two further guards sit outside that file. `openrouter_judge_proxy.py` refuses
any request whose `max_tokens` is not the frozen bound, so pointing an
unmodified upstream evaluator at the proxy fails loudly instead of returning a
plausible 0%. And `local-rehearsal.py` reads the evaluator's SHA-256 **out of
the built image** and compares it with the digest above, so a fork revision and
a recorded digest that disagree stop the rehearsal.

## Reading a number produced this way

A result from a substituted judge is **not an official-protocol LongMemEval
score** and must not be compared with published LongMemEval numbers. Two
specifics worth carrying:

- **Agreement is not correctness.** `gpt-4o` honours the temporal task's "do
  not penalize off-by-one errors" clause on only 31/53 (58%) of the cases that
  trigger it. A successor picked on agreement alone reproduces that defect and
  calls it compatibility; `deepseek-v4.1-flash` honours it 32% of the time,
  which is *worse* than the incumbent. This fork does not resolve that — it
  makes the lane that scores miners reproducible locally. The judge choice
  itself is owned by the production change and the study under
  `docs/longmemeval-benchmark/judge-study/`.
- **`temperature=0` is not determinism.** 20/1,893 (1.06%) of byte-identical
  replicates were non-unanimous for `gpt-4o` itself, matching the published
  audit figure of 3/500. A replacement is measured against that floor, not
  against perfection.
