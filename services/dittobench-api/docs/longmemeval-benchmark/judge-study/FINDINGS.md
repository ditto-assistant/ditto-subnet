# The Longevity of LongMemEval

Nine models, two calling conventions, three replicates, 1,893 probes,
102,222 judge calls, $6.89.

Every LongMemEval score ever published is denominated in one model:
`gpt-4o-2024-08-06`. LongMemEval's official evaluator hard-codes it in a
three-entry `model_zoo`, every paper that reports a LongMemEval number inherits
it, and this subnet froze it into its v9 confirmation profile.

It retires from the OpenAI API on **2027-01-14** and from Azure on
**2027-04-14**.

So the benchmark has a dependency with an expiry date, and nobody has published
what happens when you replace it. We tried to, on 1,893 probes across nine
models and three replicates each. The three things we found were, in order of
increasing surprise, about the protocol, about the rubric, and about the
incumbent.

---

## 1. The protocol is the blocker, not the model

The official evaluator calls the judge like this:

```python
kwargs = {'model': metric_model, 'messages': [...],
          'n': 1, 'temperature': 0, 'max_tokens': 10}
completion = chat_completions_with_backoff(metric_client, **kwargs)
eval_response = completion.choices[0].message.content.strip()
label = 'yes' in eval_response.lower()
```

Ten tokens, and the verdict is a substring test. That was a reasonable design in
2024, when a chat model's first ten tokens were its answer.

It is no longer reasonable. A model that emits reasoning tokens spends the
entire budget before producing any visible content. It returns `content=""`
with `finish_reason="length"`. And `'yes' in ""` is `False`.

So the model is scored **"no"**. On every question. With no error raised
anywhere — not in the evaluator, not in the client, not in the logs.

Measured across the full suite — 1,893 probes × 3 replicates = 5,679 calls per
model, under the official calling convention:

| model | empty content | truncated at 10 tokens | silently scored "no" |
|---|---|---|---|
| `qwen/qwen3.8-flash` | 100.0% | 100.0% | 100.0% |
| `mistralai/mistral-large-4-0` | 99.3% | 99.3% | 99.3% |
| `deepseek/deepseek-v4.1-flash` | 92.0% | 94.0% | 92.0% |
| `moonshotai/kimi-k3` | 75.9% | 100.0% | 75.9% |
| `z-ai/glm-5.3-flash` | 72.4% | 72.7% | 72.4% |
| `minimax/minimax-m3.1-flash` | 30.0% | 30.0% | 30.0% |
| `openai/gpt-6-luna` | 23.9% | 24.4% | 23.9% |
| `openai/gpt-5.6-luna` | 0.4% | 0.4% | 0.4% |
| `openai/gpt-4o-2024-08-06` | 0.0% | 0.0% | 0.0% |

The mechanism is not inferred. `usage.completion_tokens_details.reasoning_tokens`
reports 9–10 of the 10-token budget consumed before any visible output, and

```json
{"reasoning": {"enabled": false}}
```

drops every one of those rates to 0.0% and produces `"yes"` or `"no"` in one or
two tokens.



The failure is silent and it is *biased*: it does not produce noise, it produces
a uniform "incorrect" verdict. An agent evaluated by a truncated judge scores
near its abstention rate, which looks like a real — if bad — result. If you were
benchmarking a memory system and swapped judges without checking, you would
conclude the system had collapsed.

`gpt-6-luna` is the worst case, because it fails *intermittently*. A judge that
returns empty content on 14 of 30 calls looks like flakiness, rate limiting, or
a bad prompt. It is none of those.

**Practical consequence:** the first thing to check when substituting a
LongMemEval judge is not agreement. It is whether the candidate emitted any
visible content at all. Assert on `finish_reason` and on empty strings, before
you compute a single accuracy number.

---

## 2. The incumbent does not follow its own rubric

LongMemEval does not use one judge prompt. It uses five, selected by question
type, and each carries specific instructions. The temporal-reasoning template
says, verbatim:

> In addition, do not penalize off-by-one errors for the number of days. If the
> question asks for the number of days/weeks/months, etc., and the model makes
> off-by-one errors (e.g., predicting 19 days when the answer is 18), the
> model's response is still correct.

That is about as explicit as a rubric gets. We built 53 probes that do exactly
what it describes: take a temporal-reasoning question whose reference answer is
a count, and shift the count by one.

**`gpt-4o-2024-08-06` honours the clause on 31 of 53 (58%).**

It is not an artifact of how the probes were built. Restricted to the clause's
own trigger condition — questions that literally ask "how many" or "how long",
52 of the 53 — it honours the clause **57.7%** of the time. With an explicit
unit in the hypothesis ("17 days" rather than "17"), 61.9%.

Concrete cases, all judged "no":

| question | reference | hypothesis |
|---|---|---|
| How many months before my anniversary did Rachel get engaged? | `2` | `1` |
| How many weeks have I been taking sculpting classes…? | `3` | `2` |
| How many days had passed since I finished reading…? | `18 days. 19 days (including the last day) is also acceptable.` | `17 days` |

The last one is notable: the reference answer itself already names a tolerance.

The benchmark's temporal-reasoning accuracies are therefore systematically lower
than the benchmark's own stated intent. Not by a rounding error — on the
subpopulation the clause governs, roughly two in five verdicts go the wrong way.
Temporal-reasoning is 133 of the 500 questions.

---

## 3. The incumbent is not deterministic

`temperature=0` is not determinism; it is a sampling parameter. Running the full
1,893-probe suite three times through `gpt-4o-2024-08-06` with byte-identical
inputs:

**20 of 1,893 probes (1.06%) did not return a unanimous verdict.**

This is consistent with the independent audit that found 3 flips in 500 on
re-judging identical inputs. It means any LongMemEval comparison has an
irreducible noise floor, and that two systems separated by less than roughly a
percentage point are not distinguishable by this benchmark no matter how
carefully you run it.

It also sets the bar for a replacement. A candidate judge should not be
compared against perfect agreement with 4o. It should be compared against **4o's
agreement with itself**, which is 98.9%.

---

## 4. Which model should replace it

Two arms per model. **`@official`** is the official calling convention
untouched. **`@noreason`** adds `reasoning: {"enabled": false}` and changes
nothing else — same prompt, same `max_tokens=10`, same label extraction.

### Agreement with the incumbent

Modal verdict over three replicates, 1,893 probes:

| judge | agree | κ | yes-bias | Youden J | $/M in |
|---|---|---|---|---|---|
| `gpt-4o-2024-08-06` @noreason | 99.3% | 0.985 | −0.4% | 0.986 | 2.50 |
| **`qwen3.8-flash` @noreason** | **97.9%** | **0.957** | −2.1% | 0.963 | 0.15 |
| `mistral-large-4-0` @noreason | 97.8% | 0.955 | −1.3% | 0.959 | 1.36 |
| `gpt-5.6-luna` @noreason | 96.9% | 0.936 | +1.4% | 0.932 | 0.20 |
| `gpt-6-luna` @noreason | 96.7% | 0.932 | +2.0% | 0.926 | 0.10 |
| `deepseek-v4.1-flash` @noreason | 95.9% | 0.916 | +1.5% | 0.912 | 0.02 |
| `kimi-k3` @noreason | 95.6% | 0.910 | −4.3% | 0.923 | 0.24 |
| `gpt-6-luna` @official | 93.4% | 0.868 | −6.6% | 0.887 | 0.10 |
| `minimax-m3.1-flash` @official | 92.4% | 0.849 | −7.4% | 0.869 | 0.15 |
| `glm-5.3-flash` @official | 59.5% | 0.265 | −40.5% | 0.300 | 0.15 |
| `kimi-k3` @official | 54.8% | 0.190 | −45.2% | 0.218 | 0.24 |
| `deepseek-v4.1-flash` @official | 45.2% | 0.042 | −54.1% | 0.049 | 0.02 |
| `mistral-large-4-0` @official | 42.3% | 0.000 | −57.7% | 0.000 | 1.36 |
| `qwen3.8-flash` @official | 42.2% | 0.000 | −57.8% | 0.000 | 0.15 |

The `@official` rows are the §1 failure, quantified. `qwen3.8-flash` and
`mistral-large-4-0` reach **κ = 0.000** — literally zero information beyond the
base rate — and the identical models with one flag flipped are the two best
candidates in the panel.

### Two models cannot be fixed by the flag

`z-ai/glm-5.3-flash` and `minimax/minimax-m3.1-flash` rejected
`reasoning: {"enabled": false}` on **5,679 of 5,679 calls**:

```
400 {"error":{"message":"Reasoning is mandatory for this endpoint
     and cannot be disabled."}}
```

They are structurally incompatible with the official protocol. The only remedy
is raising `max_tokens`, which means editing LongMemEval itself and forfeiting
protocol equivalence. Worth stating plainly because `glm-5.3-flash` is
otherwise an attractive candidate and is already this repo's screener
adjudicator model.

### Stability

Several candidates are **more stable than the incumbent**:

| judge | non-unanimous / 1,893 | flip rate |
|---|---|---|
| `mistral-large-4-0` @noreason | 2 | 0.11% |
| `qwen3.8-flash` @noreason | 10 | 0.53% |
| **`gpt-4o-2024-08-06` @official** | **20** | **1.06%** |
| `kimi-k3` @noreason | 21 | 1.11% |
| `gpt-5.6-luna` @noreason | 24 | 1.27% |
| `deepseek-v4.1-flash` @noreason | 25 | 1.32% |
| `gpt-6-luna` @noreason | 37 | 1.95% |
| `gpt-6-luna` @official | 73 | 3.86% |
| `glm-5.3-flash` @official | 77 | 4.07% |
| `minimax-m3.1-flash` @official | 200 | 10.57% |

The truncated arms are the unstable ones, which makes sense: the reasoning/
content boundary at exactly ten tokens is marginal, so the same input lands on
either side of it run to run. `minimax-m3.1-flash@official` flips on one probe
in ten.

### The result that actually matters

Rank by agreement with 4o and `qwen3.8-flash` wins. Rank by conformance to
LongMemEval's own rubric and it is near the bottom:

| judge | clause conformance | off-by-one clause (§2) |
|---|---|---|
| `gpt-6-luna` @noreason | **98.8%** | **83%** |
| `gpt-5.6-luna` @noreason | 98.4% | 66% |
| `gpt-4o-2024-08-06` @official | 98.0% | 58% |
| `mistral-large-4-0` @noreason | 97.1% | 27% |
| `qwen3.8-flash` @noreason | 96.9% | **6%** |
| `deepseek-v4.1-flash` @noreason | 96.8% | 32% |
| `kimi-k3` @noreason | 94.8% | 6% |

**`qwen3.8-flash` agrees with the incumbent partly because it reproduces the
incumbent's defect.** It almost never forgives an off-by-one, and neither does
4o, 42% of the time. `gpt-6-luna` agrees *less* — and a good share of that
disagreement is it following the written rule that 4o ignores.

So "closest to 4o" and "most faithful to LongMemEval" are different objectives
that point at different models. A drop-in replacement selected on agreement
alone locks in a known defect, permanently, and calls it compatibility.

### Recommendation

There is no single answer, so here are two, with the trade stated:

- **For continuity** — scores that stay comparable to the published record:
  **`qwen/qwen3.8-flash` with reasoning disabled.** κ = 0.957, Youden 0.963,
  half the incumbent's flip rate, **16.7× cheaper**, and open-weight. Accept
  that it inherits the off-by-one defect.
- **For correctness** — scores that mean what the benchmark says they mean:
  **`openai/gpt-6-luna` with reasoning disabled.** Highest clause conformance in
  the panel (98.8%), 83% on the off-by-one clause against the incumbent's 58%,
  **25× cheaper**. It is not open-weight, and it will *raise* scores by roughly
  three points, so it is not drop-in comparable.

**No open-weight model in this panel honours the off-by-one clause well.** The
best is `deepseek-v4.1-flash` at 32%, against 4o's 58% and `gpt-6-luna`'s 83%.
If an open-weight judge is a requirement, continuity is currently the only
option on offer, and the rubric gap is the price.

For a subnet ranking miners against historical scores, continuity wins: the
leaderboard is a relative ordering, and changing the denominator mid-flight
invalidates every prior comparison.

## 5. Translating old scores to a new judge

Given a candidate judge J with, relative to the incumbent,

- α = P(J says yes | 4o says yes) — sensitivity
- β = P(J says no | 4o says no) — specificity

an agent whose 4o-denominated accuracy is S scores, under J:

&nbsp;&nbsp;&nbsp;&nbsp;`S_J = α·S + (1 − β)·(1 − S)`

and the inverse recovers the old denomination:

&nbsp;&nbsp;&nbsp;&nbsp;`S_4o = (S_J − (1 − β)) / (α + β − 1)`

This is the Rogan–Gladen prevalence correction. The denominator is Youden's J,
and it is the quantity that decides whether translation is possible at all. At
J = 0.963 (`qwen3.8-flash@noreason`) a measured score converts back with a small
multiplier on the error. At J = 0.000 (`qwen3.8-flash@official`) there is
nothing to convert: the judge emitted no information, and no formula recovers a
score from it.

Applying α and β estimated **per question type** to the frozen 2026-07-23
leaderboard projects what those five agents would have scored under each judge:

| judge | whitycatboss | killer-4 | ditto-max-v1 | avadakedavra | zeus_v12 |
|---|---|---|---|---|---|
| **gpt-4o (actual)** | **20.2%** | **53.0%** | **57.6%** | **6.2%** | **35.6%** |
| `gpt-4o` @noreason | 20.0% | 52.6% | 57.4% | 6.5% | 35.5% |
| `qwen3.8-flash` @noreason | 18.5% | 50.8% | 56.1% | 5.8% | 34.3% |
| `mistral-large-4-0` @noreason | 18.9% | 51.0% | 56.5% | 6.5% | 34.8% |
| `gpt-5.6-luna` @noreason | 22.9% | 55.0% | 60.0% | 9.9% | 38.4% |
| `gpt-6-luna` @noreason | 23.7% | 55.7% | 60.9% | 11.0% | 39.3% |
| `deepseek-v4.1-flash` @noreason | 23.8% | 55.4% | 60.3% | 10.8% | 39.0% |
| `gpt-6-luna` @official | 16.8% | 46.6% | 51.7% | 5.2% | 31.5% |
| `glm-5.3-flash` @official | 5.6% | 16.0% | 17.7% | 1.7% | 10.7% |
| `deepseek-v4.1-flash` @official | 2.0% | 3.6% | 3.6% | 1.1% | 2.6% |
| **`qwen3.8-flash` @official** | **0.0%** | **0.0%** | **0.0%** | **0.0%** | **0.0%** |

The ordering of the five agents survives every viable judge. The *values* do
not: the spread between the best and worst viable candidate is about five
points on a 57.6% agent. And the bottom row is §1 in its final form — a
benchmark run that completes successfully, reports zero for every agent, and
raises no error.

Two limits, stated plainly:

- α and β **must** be per question type. Conformance varies enormously by type,
  so a single global pair mis-translates temporal-reasoning in one direction and
  knowledge-update in the other.
- These coefficients come from an adversarial probe suite, deliberately
  concentrated on the boundaries where judges disagree. The table above is a
  **projection, not a measurement**. Validating it needs the original hypothesis
  text from those runs, which was not retained — only `hypotheses_sha256`.
  Publishing hypotheses alongside scores would make this directly checkable, and
  nobody in this field does it.

---

## Method

Everything here is reproducible from public inputs. The probe suite is
constructed deterministically from the 500 public LongMemEval questions and a
fixed seed, with no model in the loop; gold labels are read off the official
prompt templates rather than assigned by anyone's judgement. The runner
reimplements the official `get_anscheck_prompt` verbatim and reproduces the
official call exactly.

See `README.md` in this directory for pins, construction hazards, and the one
gold-label defect the incumbent itself exposed.
