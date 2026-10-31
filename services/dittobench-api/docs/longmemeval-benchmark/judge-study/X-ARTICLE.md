# X publication assets

Blog: `landing-astro` PR #463 → heyditto.ai/blog
Backing data: ditto-subnet PR #3144

---

## A. Thread (10 posts)

**1/**
Every LongMemEval score ever published is denominated in one model:
gpt-4o-2024-08-06.

It's hard-coded in the official evaluator.

It retires from the OpenAI API on 2027-01-14. Azure 2027-04-14.

Nobody has published what happens when you swap it. So we did.

102,222 judge calls. $6.89. 🧵

**2/**
First finding, and it's the one that should worry you.

LongMemEval calls the judge with max_tokens=10 and reads the verdict as:

    label = 'yes' in response.lower()

A reasoning model spends all 10 tokens thinking, returns content="", and
`'yes' in ""` is False.

So it's scored **no**. Every question.

**3/**
How often, across 5,679 calls per model under the official protocol:

qwen3.8-flash — 100% empty
mistral-large-4 — 99.3%
deepseek-v4.1 — 92%
kimi-k3 — 75.9%
glm-5.3-flash — 72.4%
gpt-6-luna — 23.9%
gpt-4o — 0%

No error is raised. Anywhere.

**4/**
Two things make this nastier than it looks.

The failure is **biased, not noisy** — your system scores near its abstention
rate, which looks like a real (bad) result. You'd debug your memory system.

And gpt-6-luna fails *intermittently* at 24%. That reads as flakiness, not
as a broken config.

**5/**
The fix is one line:

    {"reasoning": {"enabled": false}}

Every rate above → 0.0%.

Except two models that answer:
"Reasoning is mandatory for this endpoint and cannot be disabled."
5,679 of 5,679 calls. Structurally incompatible with the protocol.

**6/**
Second finding: the incumbent doesn't follow its own rubric.

LongMemEval's temporal prompt says, verbatim: "do not penalize off-by-one
errors for the number of days."

gpt-4o honours that on 31 of 53 probes. **58%.**

Temporal reasoning is 133 of the 500 questions.

**7/**
One case it marked wrong:

Correct answer: "18 days. 19 days (including the last day) is also acceptable."
Model answered: "17 days"
Judge: ❌

The reference answer itself declares a tolerance. The judge ignores it.

**8/**
Third: temperature=0 is not determinism.

Same 1,893 probes, 3 identical runs through gpt-4o:
**20 (1.06%) came back non-unanimous.**

So LongMemEval has a noise floor. Two systems within ~1 point are not
distinguishable by it, however carefully you run it.

**9/**
Now the part we didn't expect.

Rank candidates by agreement with 4o → qwen3.8-flash wins (κ 0.957).
Rank by conformance to LongMemEval's rubric → it's near last (6% on the
off-by-one clause).

**It agrees with 4o partly by reproducing 4o's mistake.**

**10/**
So "closest to 4o" and "most faithful to the benchmark" are different
objectives selecting different models.

Continuity → qwen3.8-flash +noreason. κ 0.957, 16.7× cheaper, open-weight.
Correctness → gpt-6-luna +noreason. 83% on the clause vs 4o's 58%, 25× cheaper.

Pick deliberately. Full data ↓

---

## B. Long-form X article

### Title
The benchmark everyone uses for AI memory is about to lose its judge

### Subtitle
We ran 102,222 judge calls to find out what breaks. The answer wasn't the
models — it was the protocol, and it fails silently.

### Body

Use the blog post at heyditto.ai/blog verbatim. It is written to stand alone
and is already structured for long-form: three numbered findings, a
recommendation with the trade stated, and a method section.

Two edits for the X version:
- Cut the "Method, and how to check us" section down to its first paragraph
  and a link, since X readers won't reproduce it inline.
- Move the "If you take one thing from this post" callout from section 1 up
  into the subtitle region, because on X the first screen is the whole
  decision.

### Reply-ready points

**"Isn't this just a config issue?"**
Yes — and that is the finding. It is a config issue that produces a
plausible-looking score instead of an error, in a benchmark whose results are
published without the config attached.

**"Why not just raise max_tokens?"**
You can, and then your scores are no longer protocol-equivalent to anyone
else's. That is a real cost in a benchmark whose whole value is comparability.
Worth paying — but worth paying knowingly.

**"Does this invalidate published LongMemEval numbers?"**
No. Numbers produced with 4o under the official protocol are internally
consistent. What it means is: (a) temporal-reasoning accuracies sit below the
rubric's intent, (b) there is a ~1 point noise floor, and (c) anyone
substituting a judge from here on needs to check their judge is answering.

**"Which should we actually use?"**
If you're comparing against the published literature, continuity
(qwen3.8-flash, reasoning off). If you're measuring a system for your own
decisions, correctness (gpt-6-luna, reasoning off) and re-baseline.

### Disclosure to keep in the post

The projected-leaderboard table is a projection, not a measurement. The
coefficients come from an adversarial probe suite. Validating it needs the
original model answers, which we hashed but did not keep — standard practice,
and exactly why this is hard for anyone to check. We're changing that on our
side.
