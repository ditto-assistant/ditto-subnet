# Source-review architecture review (2026-09-08)

Trigger: the 2026-09-06 aceron_v13/v14 review found the automated court
admitting the champion without a review. This note quantifies the four-layer
design as it actually ran, names why it is unreliable, and recommends the
shape to move to. Numbers come from the Backroom quarantine log
(2026-08-31T17:00Z, policy v11 activation, to 2026-09-08T02:32Z; 311 court
outcomes) and 48 hours of `ditto-screener-worker@*` journals on
`subnet-screener-1` (133 screenings).

## What the four layers actually produced

| Court outcome (311 rows) | Count | Share |
| --- | --- | --- |
| Fail-open clear (court refused, wrapper cleared) | 222 | 71% |
| Certified clear | 24 | 8% |
| Certified reject | 10 | 3% |
| Other (oracle passed 31, challenge HTTP failure 15, inconclusive 3, escalate 4, misc 2) | 55 | 18% |

Of the 222 fail-open clears, 141 carried zero review notes: no layer produced
any evidence before the row was admitted. The two rows with the most L1 notes
(new-mind v2, 48 notes; dii-v2, 46 notes) were still admitted by a court crash.

Journal view, last 48 h on the fleet node (133 screenings, 115 pass, 11
quarantine, 2 retryable, 0 reject):

| Layer | Observed |
| --- | --- |
| L1 (Luna, `/chat/completions`) | 102 HTTP 429 responses, 11 connection errors, 18 budget-terminated reviews |
| L2 analyst (Terra, `/responses`) | 4 trajectory failures, 3 model-budget exhaustions |
| L3 critic (Sol) | ran 19 times out of 133 |
| L4 court (GLM 5.3 flash) | 71 failures: 35 timeouts, 20 "decision-only response omitted final tool call", 7 "exceeded lease budget", 6 connection errors, 3 JSON decode errors; 22 verdicts self-inconsistent with the `SourceReviewAdjudication` contract; **0 certified decisions** |

Shadow observations for the L2/L3 lane (last 100): retryable infra 73,
violation 26, inconclusive 1; critic not run 70. When the analyst/critic lane
ran it either hit infrastructure or found a violation; it produced no clears.
Every terminal clear in the window came from the L4 wrapper settling a
refusal, which #1714 has since stopped.

Current models and budgets (global revision 105): L1 `openai/gpt-5.6-luna`
(160 steps, 8 MB, 8000 tokens, 600 s); L2 `openai/gpt-5.6-terra` with fallbacks
`z-ai/glm-5.2`, `openai/gpt-5.6-sol` (18 steps, $5 cap); L3 `openai/gpt-5.6-sol`
(medium effort); L4 `z-ai/glm-5.3-flash` (128 steps, 600 s, 90 s per call).
Note: node-scope revision 101 for `subnet-screener-1` still pins
`l2_model=moonshotai/kimi-k3`, `adjudicator_mode=off`, `profile=core`; it is
`mode=inherit` today so the global revision wins, but the stale override
resurfaces the moment the node is flipped to `enforce`.

## Why it is unreliable

1. **One upstream, four dependent hops inside one 15-minute lease.** Every
   layer calls OpenRouter; a 429 burst on L1 consumes the review partition,
   L2 exhausts its model budget, and the court inherits a window of about
   30 seconds. Any single hop failing cascaded into a refusal.
2. **The court model cannot meet the verdict contract.** 49 of 71 court
   failures were model-side: GLM 5.3 flash omitted the required final tool
   call, exceeded its budget, or returned a verdict that contradicts itself
   (clear while naming a breached invariant, reject without executable
   citations). A strict, citation-verified contract is the right design; a
   small fast model is the wrong executor for it.
3. **A refusal was an admission** (fixed by #1714). This turned every
   failure above into a clean policy stamp.
4. **The middle layers add hops, not decisions.** L2/L3 exist to critique
   L1's finding before the court; in practice they ran rarely, cleared
   nothing, and their $5 budget was spent mostly on infrastructure retries.

## Recommendation

### Collapse to two model layers plus deterministic evidence

- **Deterministic first** (already exists, keep and extend): C13 static
  fingerprints, the behavioral oracle, and the invariant probes below. These
  cost no review tokens, are reproducible, and can carry public-safe reason
  codes.
- **L1 reader** (Luna or Sol, high effort): walk the served path, write the
  cited notes ledger. Keep the budget generous enough to finish a 15k-line
  `baseline.rs` (the revision 103 cut to 40 steps / 4 MB is what produced the
  zero-note rows).
- **Court** on a frontier model, decision-only over the preloaded evidence
  with tool access to re-read: `anthropic/claude-opus-5` primary,
  `openai/gpt-5.6-sol` fallback (the miner suggestion is sound). One contract
  repair turn on a self-inconsistent verdict, then a hold, never a clear.
  Frontier tokens are more expensive per call, but the court runs once with
  preloaded evidence, and the analyst/critic spend disappears.
- **Drop L2/L3** as separate model hops. Their useful function, "does this
  lead survive a second reading", is what the court does with the ledger in
  front of it.

Touchpoints for the model change: `review_settings.py` literals (worker),
`api_models/screener_review_settings.py` (platform), `admin.schemas.ts`
(Backroom), the protocol package, plus a provider that routes Anthropic
models (Ditto Inference does; OpenRouter does too).

### Probing: use the isolated gateway as a witness

The screener already runs the harness against a fake OpenAI-compatible
gateway (behavioral oracle). That gateway sees exactly what the harness sends
the model and controls exactly what the model says back, which makes the
harness's post-model behaviour testable without reading a line of source:

| Probe | Gateway reply | Violation if served `answer` differs |
| --- | --- | --- |
| Slot rescale (I4) | `ANSWER: 260195` to a cents question | `2601.95` proves a host `/100` rewrite |
| Direction map (I4) | `ANSWER: loss; 744.62` | `decrease; 744.62` proves the synonym map |
| Slot composition (I4) | prose with a number, no `ANSWER:` line | any populated `answer` proves host composition |
| Dissent/abstain gate (I1/I3) | "I cannot determine this from the records" | an answer proves the model was overruled |
| Tool-plan enforcement (I7) | tool call outside the harness's plan | the call not executing proves a forced plan |
| Prompt sheets (I5) | none needed | the captured prompt contains `MONETARY RESPONSE CONTRACT`, minor-unit conversion rules, or family formats |

Implement as a `BehavioralInvariantProbeModule` with a rotating private pack,
tripwire to quarantine while calibrating, then deterministic reject for exact
rewrite matches. This is the cheapest, most reproducible evidence the
screener can produce, and it targets exactly the class that emptied the board
on 2026-09-06.

### Reliability and visibility

- Move review traffic off the shared OpenRouter path (#1693, Ditto
  Inference), or at minimum give the court its own provider.
- Surface per-layer outcome counts and the refusal rate in Backroom; the 71%
  figure above should have been a dashboard tile, not a forensic result.
- Retire node-scope revision 101 or re-issue it with values matching the
  global revision.

## Status of the immediate fixes

- #1714 (live 2026-09-08 01:13Z): a refusal is an operator hold, never a
  clear; court per-call cap 45 s -> 90 s; node-local court outage retries.
- #1693 (open): all four layers on Ditto Inference behind one switch.
- #1717/#1719: Secret Manager container and grants for the Ditto review key.
- Fail-closed backfill: platform PR in progress to hold agents admitted by a
  fail-open clear until an operator reviews them; the 22 ranked such agents
  were held by hand on 2026-09-08.
