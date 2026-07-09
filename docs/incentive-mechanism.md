# Incentive Mechanism — Options & Trade-offs (SN118)

Draft for team discussion, 2026-06-22. Goal: pick how emissions are distributed
across miners based on benchmark scores.

## What makes this subnet different

SN118 is a **best-artifact competition**, not a live-inference subnet. Miners
submit a memory-harness implementation; validators bench it; the artifact is
**downloadable**. That changes the incentive math in two ways:

1. **Copying is the central threat.** Anyone can download the current best
   harness and resubmit it (verbatim or lightly tweaked). The mechanism must not
   pay copiers, or the subnet degenerates into copy-farming.
2. **Improvement is discrete, not continuous.** Scores jump when someone ships a
   genuinely better harness, then plateau. The mechanism should reward *beating
   the state of the art*, not occupying a rank.

Every option below is judged primarily on: **anti-copy**, **drive to improve**,
**miner participation/retention**, **gaming resistance**, and **complexity**.

---

## Option A — KOTH / Pure Winner-Take-All + ATH gate

Top miner takes ~all emissions. A challenger only dethrones the incumbent by
beating its score by a margin (e.g. **1%**). First-to-submit wins ties.

**Pros**
- Strongest anti-copy: a copy *ties* the incumbent, never beats the margin, so it
  earns nothing. First-seen timestamp protects the original.
- Maximizes drive for genuine SOTA improvement.
- Dead simple to reason about and to explain to miners.
- Emission stability: incumbent holds until truly beaten.

**Cons**
- Brutal on participation — only #1 earns, so losers leave; subnet can hollow out.
- Whale/timing risk: a single great submission locks others out for a long time.
- "Cliff" dynamics: a 0.9% improvement earns nothing, a 1.1% earns everything.

---

## Option B — Top-K equal split (e.g. top 5)

Top K miners split emissions equally.

**Pros**
- Best participation/retention — more miners earn, subnet stays populated.
- Softer dynamics; less timing luck.

**Cons**
- **Worst anti-copy:** copy the winner, land in the top K, collect a full share.
  Requires aggressive plagiarism detection to be viable at all.
- Weak drive to improve — being "good enough for top 5" pays the same as #2.
- Flat split ignores how much better #1 is than #5.

---

## Option C — Top-3 weighted, 70:20:10

Ranked split among the top 3.

**Pros**
- Compromise: rewards the winner heavily while keeping #2/#3 in the game
  (better retention than pure WTA).
- Simple, legible to miners.

**Cons**
- Still copy-exposed: a copy at #2/#3 earns 20%/10% for zero new work.
- Fixed ratios are arbitrary and need re-tuning as the field changes.
- Modest improvement incentive vs. KOTH (you can coast at #2).

---

## Option D — Pareto frontier (multi-objective)

Score on multiple axes (quality, token cost, wall-clock); reward the
**non-dominated** set, split by some rule (equal, or by hypervolume contribution).

**Pros**
- Matches the real product goal — there's no single "best"; a cheap-fast harness
  and a max-quality harness can both be valuable.
- Encourages specialization and a diverse frontier, not one monoculture.
- Harder to copy your way onto the frontier across *all* axes at once.

**Cons**
- Most complex to implement, explain, and keep deterministic for consensus.
- Frontier membership can be unstable; emissions jitter as points enter/leave.
- Axis weighting/normalization becomes a new gameable surface.

---

## Option E — Score-proportional / softmax

Emissions ∝ score (or softmax over scores with a temperature).

**Pros**
- Smooth, continuous; no cliffs; everyone with a real score earns something.
- Naturally rewards being better, proportionally.

**Cons**
- Copy-exposed (a copy gets the same proportional share as the original).
- Temperature is a tuning knob that's easy to get wrong (too flat = no incentive,
  too sharp = de facto WTA).
- Encourages sybil/spam of near-duplicate submissions to farm the tail.

---

## Comparison at a glance

| Mechanism | Anti-copy | Drive to improve | Participation | Gaming resist | Complexity |
| --- | --- | --- | --- | --- | --- |
| A. KOTH + ATH gate | ★★★★★ | ★★★★★ | ★★ | ★★★★ | ★ (low) |
| B. Top-5 equal | ★ | ★★ | ★★★★★ | ★★ | ★★ |
| C. Top-3 70:20:10 | ★★ | ★★★ | ★★★★ | ★★ | ★★ |
| D. Pareto frontier | ★★★★ | ★★★★ | ★★★ | ★★★ | ★★★★★ (high) |
| E. Proportional/softmax | ★★ | ★★★ | ★★★★ | ★★ | ★★★ |

---

## Recommendation

**Start with A (KOTH + ATH gate) as the core, add a small participation tail.**

Rationale: anti-copy is the existential risk for a downloadable-artifact subnet,
and KOTH+ATH is the only option that's structurally resistant — a copy can't beat
the margin, and first-seen protects the original. Pure WTA's one weakness
(participation) is cheap to patch with a **small fixed tail** (e.g. 90% to the
ATH holder, 10% spread over the next few distinct, non-plagiarized submissions)
without reopening the copy hole.

**Then evolve toward D (Pareto)** once the bench is mature and we actually have
multiple objectives (quality vs cost vs latency) worth trading off. Pareto is the
right *long-term* shape because it matches the product, but it's too complex and
too jittery to launch on.

In all cases the mechanism needs, as non-negotiable supports:
- **First-seen timestamps** + **plagiarism / near-duplicate detection**, and
- a **deterministic** scoring function (so validators converge on identical
  weights — see D3 in `PROJECT.md`).

Avoid B and E as the primary mechanism: both pay copies and underweight genuine
improvement.
