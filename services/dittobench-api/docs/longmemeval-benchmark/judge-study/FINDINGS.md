# The Longevity of LongMemEval

*Draft — the nine-model panel is still running; §4 and §5 are incomplete.*

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

Measured against the official protocol, 30 calls per model:

| model | empty content | reasoning tokens used of 10 |
|---|---|---|
| `mistralai/mistral-large-4-0` | 30/30 | 9 |
| `qwen/qwen3.8-flash` | 30/30 | 10 |
| `deepseek/deepseek-v4.1-flash` | 28/30 | 10 |
| `moonshotai/kimi-k3` | 25/30 | — |
| `z-ai/glm-5.3-flash` | 24/30 | — |
| `openai/gpt-6-luna` | 14/30 | 0 (intermittent) |
| `openai/gpt-4o-2024-08-06` | 0/30 | 0 |

With `reasoning: {"enabled": false}` every one of those models answers `"yes"`
or `"no"` in one or two tokens.

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

*Pending — full panel running.*

## 5. Translating old scores to a new judge

Given a candidate judge J with, relative to the incumbent,

- α = P(J says yes | 4o says yes) — sensitivity
- β = P(J says no | 4o says no) — specificity

an agent whose 4o-denominated accuracy is S scores, under J:

&nbsp;&nbsp;&nbsp;&nbsp;`S_J = α·S + (1 − β)·(1 − S)`

and the inverse recovers the old denomination:

&nbsp;&nbsp;&nbsp;&nbsp;`S_4o = (S_J − (1 − β)) / (α + β − 1)`

This is the Rogan–Gladen prevalence correction. The denominator is Youden's J.
As it approaches zero the translation becomes unstable — which is the honest way
of saying that a judge below some agreement threshold destroys too much signal
to convert back, no matter what formula you apply.

Two limits worth stating plainly:

- α and β must be estimated **per question type**, because conformance varies
  enormously by type (see §2). A single global pair will mis-translate
  temporal-reasoning in one direction and knowledge-update in the other.
- α and β estimated on an adversarial probe suite do not automatically transfer
  to a realistic mixture of agent outputs. The probes are deliberately
  concentrated on the boundaries where judges disagree. Translating a real
  leaderboard with probe-derived coefficients is a **projection**, not a
  measurement, and we label it as such.

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
