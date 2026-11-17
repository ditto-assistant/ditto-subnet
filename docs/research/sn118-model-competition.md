# SN118 model competition: a fixed harness, miners submit a model

Status: **research memo, 2026-11-17.** Nothing here changes a live score,
weight, setting, lease or piece of infrastructure. Infrastructure is described
as AWS proposals only. Every decision is listed under *Open questions for
Peyton*.

## TL;DR

- **The inversion is smaller than it sounds.** SN118 already locks the model
  and lets miners own the harness. Every grant pins a chat model
  (`openai/gpt-oss-20b`) and, from bench 15, a decision model
  (`typesafe/jev-1.14`). A model competition pins the harness instead and lets
  a miner artifact fill one of those two model slots. Seeds, CRN, quorum,
  dueling seasons, the sequential dethrone, outage voiding, copy holds and the
  generator competition all carry over.
- **"Mercury 3" is probably Mercury Decide.** Mercury 3 itself is Inception's
  closed generative diffusion LLM (released Oct 2026). The "classifier model
  that can do strings" Peyton describes matches **Mercury Decide**, released
  three weeks earlier. It is a Jev-shaped decision model (`noul` / `choice` /
  `score`) with an added `extract` question that returns exact text spans.
  Training our own Mercury 3 is not realistic. Training our own Mercury
  Decide-class model is realistic and cheap: open recipes fine-tune a 4B base
  with LoRA in under an hour, for tens of dollars.
- **Prior art warns against naive "submit a model" designs.** On Bittensor:
  - SN9's original competition was relaunched as IOTA.
  - SN37 was handed off.
  - SN11 Dippy reset scoring several times, then moved to image inference.
  - SN120 Affine now takes training batches instead of models.

  The designs that survive share three traits: the operator executes the
  evaluation on its own hardware, the evaluation data is fresh or private, and
  the crown passes only by a statistical test. SN118 already has all three.
- **Recommended v1: "Decide track", shadow first.**
  - Miners submit weights for the **decision slot**: a LoRA adapter, or a
    full fine-tune within one pinned small base family.
  - Ditto serves them on a Ditto-owned inference pool, with the System One wire
    plus `extract`, prefill-only and batch-invariant.
  - A frozen reference harness calls that pool in place of Jev. The chat slot
    stays `gpt-oss-20b`.
  - KOTH on the existing dueling machinery, in its own track, at 0 bps until
    the board has depth.
  - The chat slot (v2) and a single "Ditto Mercury" model that decides,
    extracts and writes (v3) follow only if v1 shows the model lever moves the
    bench.
- **Rough cost.** About $0.05–0.50 of GPU per scored run for a ≤2B decision
  model. The fleet needs about 1–2 always-on L40S-class GPUs, roughly
  $1.4k–2.7k/month on-demand. That is the same order as or below today's
  OpenRouter spend. A miner pays $5–150 per training attempt plus the
  0.075 TAO evaluation fee.
- **Gate before building.** A model-sensitivity study (P0): hold the harness
  fixed and swap decision models. If the composite spread between decent
  decision models is not several times the dethrone margin, the decision slot
  is too weak a lever, and we lead with the chat slot instead.

## 1. Where SN118 is today, and what changes

Live state from the public API (`subnet-status`, 2026-11-17):

- Scoring bench **v18** (activated). Harness model `openai/gpt-oss-20b`,
  locked, reasoning effort `medium`.
- Miner pool: memory track 70%, generator track 30%. Burn and Gamma are 0%
  under allocation revision 2.
- Memory KOTH curve 50/20/13/10/7, dethrone margin 0.007, z = 1.64.
- Champion composite 0.831, fifth place 0.741.

Today, as [MINER.md](../MINER.md#scoring-and-emissions) puts it, "Production
locks every harness to one consensus model, so model choice is not a miner
lever." The relay already forces the model per grant, and the
[inference request contract](../MINER.md#inference-request-contract) pins
`model`. From bench 15 a second, separately pinned lane exists. The decision
lane ([services/model-relay/README.md](../../services/model-relay/README.md))
forces `typesafe/jev-1.14` on a System One body `{state, questions}` and meters
input at $0.042/M (output free). It has its own walls, and outages map to the
no-fault `model_relay_unavailable`.

The v18 reference harness already splits its work across those two slots.
[`src/hybrid.rs`](../../miners/dittobench-starter-kit/README.md#reference-jev-decides-the-chat-model-writes-default-from-bench-18)
sends every *decision* to Jev and every *string* to the chat model:

| Step | Slot | Primitive |
| --- | --- | --- |
| Tool selection from a 1,400–2,000-tool menu (100k–200k tokens) | decision | `choice` over ≤255 options per question |
| Act or not, route (recall / act / remember / chat) | decision | `choice`, `noul` |
| Rank 24 retrieved notes, keep 8 | decision | `choice` |
| Argument values | decision | `choice` over code-extracted spans |
| Next tool / "is the request done?" | decision | `choice`, `noul` |
| Message bodies, titles, every tool call, the final reply | chat | generation |

So "miners submit a model" means choosing **which slot becomes the miner's**,
and freezing everything else, the harness included.

The [no-inference mode](../no-inference-mode.md) work already measured the
decision-only extreme. Over 1,000 v15 memory cases:

- 64.5% are extractive: the graded value appears verbatim in the evidence.
- 10% are abstentions.
- 23% are computed or composed.

The estimated ceiling for a decision-only agent was 0.65–0.80 composite. A
Jev-only prototype ran at $0.07–0.11 per 350-case run. That prototype's
biggest workaround was faking span extraction with two nested `choice`
questions, first over clauses and then over spans. A native `extract`
primitive removes that workaround.

## 2. What "Mercury 3" is

| | Mercury 3 | Mercury Decide | TypeSafe Jev 1.14 |
| --- | --- | --- | --- |
| Vendor, date | Inception, 2026-10-19 (blog metadata says 10-29) | Inception, 2026-09-30 | TypeSafe, early access 2026-09-15, 1.14 on 2026-10-14 |
| Kind | Generative diffusion LLM (chat, tools, JSON, reasoning effort) | Decision model: `noul`, `choice` (1–255 options), `score` (1–10 levels), **`extract` (1–26 fields → exact spans, char offsets, confidence, `not_stated`)** | Decision model: `noul`, `choice`, `score`, `box` (image). **No text span** |
| Architecture | Diffusion: drafts a block and refines it in a few passes. Parameter count undisclosed (">40x Mercury 1") | "Diffusion language modeling, the same broad approach used by Mercury 2.5". Probabilities read from the model's distribution | Undisclosed. No weights, no paper |
| Speed | 1,068 tok/s claimed (GPU unnamed). OpenRouter median ~160 tok/s (secondary source) | Not stated | ~70–500 ms per call (vendor claim) |
| Price | $0.20 in / $0.70 out per M (50% off now) | $0.02/M input (50% off $0.04), output free | $0.042/M input, output free |
| Weights | Closed | Closed | Closed |

Sources: [Inception blog](https://www.inceptionlabs.ai/blog/introducing-mercury-3),
[MarkTechPost](https://www.marktechpost.com/2026/10/19/inception-mercury-3-diffusion-llm/),
[The Decoder](https://the-decoder.com/inceptions-mercury-3-diffusion-model-claims-to-beat-gpt-6-luna-and-claude-haiku-5-5-at-a-fraction-of-the-wait/),
[Inception models docs](https://docs.inceptionlabs.ai/get-started/models),
[Mercury Decide docs](https://docs.inceptionlabs.ai/capabilities/decisions) (spot-checked),
[AlphaSignal on Mercury Decide](https://alphasignal.ai/news/inception-releases-mercury-decide-to-make-ai-routing-14x-faster),
[OpenRouter: What is Jev](https://openrouter.ai/blog/insights/what-is-jev/) (spot-checked),
[Mercury technical report, arXiv 2506.17298](https://arxiv.org/abs/2506.17298).

**Interpretation.** No product named "Mercury 3" from TypeSafe or OpenRouter
turned up. "A classifier model that can do strings" describes Mercury Decide:
Jev's typed decisions with calibrated probabilities, plus exact-span `extract`.
Mercury 3 is a general generative model. **Peyton should confirm which one was
meant** (open question 1). The rest of this memo treats the target as "a
Mercury Decide-class model we own", and treats Mercury 3 as the long-range
"one model does everything" stretch goal.

**Can we reproduce them?**

- *Mercury 3: no.* It is a closed frontier diffusion model of undisclosed
  size. Open diffusion LLMs exist:
  - LLaDA 8B: trained from scratch, about 130k H800-hours.
  - Dream 7B: initialized from Qwen2.5-7B, about 17k H100-hours (estimated).
  - DiffuCoder 7B and LLaDA2.0-mini (16B MoE, 1.4B active).
  - DiffusionGemma: 25B MoE, about 3.85B active, and the only one with a
    tool-call template.

  None is ultralight and strong. The cheapest AR-to-diffusion conversion
  (BYOD, a LoRA on Llama-3.1-8B in 3.2 GPU-hours) loses a lot: Qwen falls
  from 71.6% to 49.0% average and HumanEval from 0.89 to 0.30. Strict JSON
  and tool calling are diffusion LLMs' documented weak spot.
  Sources: [BYOD, arXiv 2610.41574](https://arxiv.org/html/2610.41574v1),
  [Dream, arXiv 2508.15487](https://arxiv.org/pdf/2508.15487),
  [LLaDA2.0](https://huggingface.co/inclusionAI/LLaDA2.0-mini),
  [DiffusionGemma, arXiv 2608.00146](https://arxiv.org/html/2608.00146v1),
  ["Bitter Lesson of dLLMs for agentic workflows", arXiv 2601.12979](https://arxiv.org/html/2601.12979),
  [Fast-dLLM](https://github.com/NVlabs/Fast-dLLM).
- *Mercury Decide-class: yes.* Open decision models mostly read option-letter
  logits from a small AR LLM, prefill-only:
  - Together's Tev1 is a rank-8 LoRA SFT on Qwen3.5-4B over 37,840 examples
    ([repo](https://github.com/togethercomputer/tev1), spot-checked). Its
    README warns that "logprobs are model preferences, not calibrated
    confidence."
  - An independent 82-decision ranking put Jev 1.13 at 79, Mercury Decide at
    76 and the open Kev-4B at 72
    ([kartit.net](https://www.kartit.net/blog/microsoft-decision-1-vs-every-decision-model/)).
  - Encoder routes also exist for extraction: GLiNER2 (205M, schema-driven
    span and JSON extraction on CPU,
    [arXiv 2507.18546](https://arxiv.org/pdf/2507.18546)) and Ettin encoders
    up to 1B ([HF blog](https://huggingface.co/blog/ettin)).
  - OpenRouter lists "21 decision models from 13 makers, all behind the same
    Decisions API", and most "take a Jev request with only the model changed"
    ([OpenRouter](https://openrouter.ai/blog/insights/what-is-jev/)). The
    interface is becoming a commodity. The models are where the value lies.

**Adjacent subnet.** Bittensor SN92 (REAX) has served calibrated decisions on
mainnet since 2026-10-14. Its miners currently **serve a closed catalog of
reference models**. They do not bring their own. Its model "crowning" track is
listed as paused ([reax.dev](https://reax.dev/), spot-checked). So no live
subnet runs a "miners train a decision model, scored inside an agent harness"
competition. That gap is ours to fill, and REAX is a possible future serving
outlet, not a competitor for the training lever.

## 3. Prior art: Bittensor and close analogs

Condensed. "Unverified" marks claims we could not find a primary source for.
Spot-checked rows were re-read for this memo; the rest come from a research
pass that cited each claim.

| Project | Miners submit | Evaluation | Anti-copy / anti-cheat | Crown and curve | What went wrong |
| --- | --- | --- | --- | --- | --- |
| **SN9 Pretraining (original, 2023–mid-2025)** | HF repo plus on-chain metadata commit. One model per hotkey. One commit per ~20 min | Pairwise per-batch loss win-rate on fresh FineWeb-class pages, 250-block evaluation delay. Validators needed an A100 80GB and 2 TB disk | **ε time advantage:** the earlier model's loss is scaled by (1−ε). Fixed 0.5%, later a linear decay 0.5%→0.01% over ~2 weeks. Miners were told to keep the HF repo private until the commit | Softmax at T = 0.01, about 96% to the best model. Per-size competitions with tight parameter windows (3.2–3.4B, 13.7–13.9B) | Winner-take-all "encouraged model hoarding". Progress stalled once ε became "a formidable hurdle". Relaunched as IOTA. [repo](https://github.com/macrocosm-os/pretraining), [constants](https://github.com/macrocosm-os/pretraining/blob/main/constants/__init__.py), [Epsilon Experiments](https://macrocosmosai.substack.com/p/the-epsilon-experiments-how-thresholds), [IOTA primer](https://arxiv.org/abs/2507.17766) |
| **SN37 Finetuning (2024–2025)** | Same as SN9. The commit hash is encrypted with the hotkey | Loss on synthetic data **generated after the upload** (≤4 h old, signed by validators with &gt;100k stake). Later multiple-choice and reasoning tasks. Validators needed ≥48 GB VRAM | Post-upload data, encrypted commits, private repos | Winner-take-all per competition | Momentum slowed and winner-take-all was judged "less effective". Handed to a new owner (Aurelius). [Substack](https://macrocosmosai.substack.com/p/fine-tuning-finely-tuned-how-sn37), [Oak Research](https://oakresearch.io/en/analyses/fundamentals/macrocosmos-bittensor-decentralized-open-ai) |
| **IOTA (SN9 since 2025-06)** | Work on assigned layers of one shared model (pipeline-parallel). Activations and weights via S3 | Throughput of processed activations, spot-checked. CLASP attribution (live status unverified) | Reproducibility checks, collusion detection in butterfly all-reduce | Proportional to work | Still running, 1.5B. Miners need ≥16 GB VRAM. [repo](https://github.com/macrocosm-os/iota) |
| **Templar SN3 (Gauntlet → Covenant-72B → Crusades)** | DeMo/SparseLoCo-compressed pseudo-gradients in a per-peer R2 bucket, within a timed put window | Loss before vs after one peer's update, on assigned and random data. Only ~5 peers evaluated per round, ranked by OpenSkill | Assigned-data vs random-data improvement (copiers score ~0, or negative at 72B), norm normalization | Payout ∝ (score − min)². Top 15 aggregated | Noisy scores. A bad update lands before it is down-weighted. The top validator supplies checkpoints. The 2025 exploit episodes are *unverified*. Covenant left Bittensor on 2026-04-10 (contested); SN3 is listed deprecated. [Gauntlet](https://arxiv.org/html/2505.21684v1), [Covenant-72B](https://arxiv.org/html/2603.08163), [Crusades](https://github.com/one-covenant/crusades) |
| **SN56 Gradients** | A training repo plus an exact commit. **Validators build it and train on validator GPUs.** Obfuscated code is rejected | Held-out loss per sample in knockout rounds | Winners open-sourced, plus a **manual exploit review** before a tournament closes | Boss round: the challenger needs ≥55% of decided samples **and** a mean lead ≥ max(0.01 nats, 1%), both at the lower bound of a fixed-seed one-sided 99% bootstrap, winning 5 of 6 tasks | The most mature "operator runs the miner's artifact" design. [miner.md](https://github.com/gradients-ai/G.O.D/blob/main/docs/miner.md) (not re-fetched: 503), [arXiv 2506.07940](https://arxiv.org/abs/2506.07940) |
| **SN11 Dippy** | HF model | Win rate, then a GPT-4o judge against a growing app dataset, because a static set "can be overfitted". Evaluation ran on an owner-hosted worker | Time penalty against "blatant model copying" | KOTH | Size-floor jumps (≥49 GB) created queues. All submissions were zeroed at a reset block. Now runs image inference. [docs](https://github.com/zerocrghost/dippy-bittensor-subnet/tree/main/docs) |
| **SN120 Affine, era 1** | HF model and revision on chain, hosted on Chutes | Multiple RL environments, Pareto dominance | Copying the leader was encouraged, but you had to dominate it | Winner-take-all | Superseded. [SimplyTAO](https://simplytao.ai/blog/your-simple-guide-to-affine-sn120) |
| **SN120 Affine, era 2 (live Nov 2026)** | **One training-batch file** (prompts, rollouts, rewards) in a private per-hotkey object | The validator runs one GRPO step from the published head and keeps it only if gain &gt; 0, z &gt; 1, cap-hit &lt; 0.33 and ≥100 tasks, on a private held-out set | RED files stay private, GREEN files are published byte-exact | A GREEN turn earns. The payout split across kings is in a separate doc | The mechanism is still changing. [README](https://github.com/AffineFoundation/affine) (spot-checked) |
| **SN29 Coldint** | SN9-style models | FineWeb-edu-2 | Pays for publicly shared improvements (Hall of Fame) | — | Forked because SN9 "lacked the incentive to publish small improvements". [validator](https://github.com/coldint/coldint_validator) |
| **SN64 Chutes / SN4 Targon** | Inference hosts | Targon compares logprobs against the validator's own model, plus TDX attestation | TEEs (TDX), pinned HF revision | — | Secondary sources only |
| **Prime Intellect INTELLECT-2** | RL rollouts from untrusted workers | **TOPLOC**: a locality-sensitive hash over activations that tolerates GPU nondeterminism and validates in about a minute | Detects model, prompt or precision swaps | — | INTELLECT-3 went back to a centralized 512-GPU cluster. [blog](https://www.primeintellect.ai/blog/intellect-2), [arXiv 2505.07291](https://arxiv.org/pdf/2505.07291) |
| **Gensyn Verde / Nous Psyche / Pluralis** | Training work | Refereed delegation with a bisection game (Verde, needs bitwise RepOps). Witness and Bloom-filter checks (Psyche). Compressed pipeline activations (Pluralis) | — | — | All aimed at ≥7B-scale pretraining. [Verde](https://www.gensyn.ai/research/verde-a-verification-system-for-machine-learning-over-untrusted-nodes), [Psyche](https://nousresearch.com/nous-psyche) |

### Lessons that apply to SN118

1. **Keep weights private until the commit, then until release.** On SN9 and
   SN37, copying from HF or the chain was the first attack. Uploading to a
   Ditto-owned store removes that channel entirely, and our source-release
   policy already decides when an artifact becomes public.
2. **Pass the crown by a statistical test, not a point estimate.** Gradients
   uses a bootstrap lower bound and Affine a z > 1 gate. Title duels already
   ship this: the sequential paired dethrone with α′ = 0.01
   ([title-duels.md](../title-duels.md)).
3. **Score only on data the model cannot have seen.** SN37's post-upload data
   and Dippy's warning about static sets point the same way. Our block-hash
   seeds and private generator slice already do this.
4. **The operator runs the evaluation.** Gradients, Crusades and Dippy run the
   artifact themselves. Miner-hosted endpoints (Affine era 1 on Chutes) add a
   trust layer that needs TEEs or logprob audits.
5. **Use fixed size windows and an architecture allowlist.** SN9's tight
   windows kept validator cost predictable. Dippy's size jump caused queues.
6. **Pure winner-take-all stagnates.** SN9 and SN37 both stalled. Keep a
   five-way curve, and allow building on released champions (Coldint, Affine).
7. **Do a human exploit review before paying a new champion** (Gradients).
   Our daily top-five read becomes a model-card and lineage read.
8. **Distributed training pays off at scale we do not need.** Every
   distributed run in this table targets ≥1B pretraining with heavy
   validators. A ≤4B fine-tune fits on one GPU in hours.
9. **Version scoring changes as eras.** Our `bench_version` contracts already
   do this. A `harness_version` should join them.

## 4. Candidate model targets mapped to the bench

The bench a model would face (v18, [dueling-bench.md](../../research/dittobench-datagen/docs/dueling-bench.md)):

- 350 cases: a 175-case datagen half (50 tool + 125 memory) and a 175-case
  private generator slice (5 columns × 35).
- A per-run budget of $1.00 per 350 cases.
- A 1,400–2,000-tool production catalog per seed, with 10 multi-call
  encounters among the tool cases.
- Deterministic grading with no judge.
- LongMemEval as a separate confirmation dimension.

| Workload in the bench | Share | (a) Ultralight LLM alone (chat slot) | (b) Decision/extract model + light LLM | (c) One "Mercury-like" model |
| --- | --- | --- | --- | --- |
| Tool selection over a 100k–200k-token menu | most of 50 tool cases | Needs harness retrieval first. A 0.6–4B model with a 32k+ window cannot read the menu | **`choice` is exactly this.** Prefill-only, ≤255 options per question | Same as (b) |
| Arguments that are spans (ids, emails, dates, amounts) | most tool cases | Generation (JSON fragility at small sizes) | **`extract`** over the request, notes and tool results | Same |
| Multi-call encounters (next tool, done?) | 10 of 50 tool cases | Generation loop | `choice` + `noul` per hop | Same |
| Memory: extractive answers | ~64% of memory | Generation, with the risk of paraphrasing away an exact value | **`extract`** with offsets | Same |
| Memory: abstention | ~10% | Prompting | `noul` → `abstain` | Same |
| Memory: computed number or money | ~14% | Weak at ≤4B | Host arithmetic, `choice` picks the reading | Same |
| Conversational, declarative acks, composed replies | ~11% | Native | Light LLM writes (or a template around the chosen spans) | Native |
| Restraint (no-call, clarify, decline) | gate-exposed | Prompting | `choice` over route plus `no_tool` | Same |

| Target | Concrete bases (Nov 2026) | Artifact | Serving cost | Lever on the bench | Main risk |
| --- | --- | --- | --- | --- | --- |
| **(a) Ultralight LLM** in the chat slot | Qwen3.5-0.8B/2B/4B (Apache 2.0, 262K context; 4B: BFCL-V4 50.3, TAU2 79.9). Gemma 4 E2B/E4B (Apache 2.0). Granite 4.0 1B/3B (Apache 2.0). Phi-4-mini (BFCL 70.3) | 2–10 GB | Decode-heavy, about $0.5–1.5 per run (section 7) | Replaces `gpt-oss-20b` (20B MoE). Probably a lower ceiling than today's chat agents | Tool-call JSON validity at small sizes. Decode nondeterminism. Biggest per-run cost |
| **(b) Decision + extract model** in the decision slot (recommended v1) | Qwen3.5-0.8B/2B (the same base open decision models use), Granite 4.0 1B, a GLiNER2-style encoder for extract | 50–500 MB as LoRA, 2–4 GB as full weights | **Prefill-only**, about $0.05–0.5 per run | Every decision row in the hybrid table. Also makes no-chat mode viable | The lever may be weaker than the chat slot. P0 measures it |
| **(c) "Ditto Mercury"**: one model that decides, extracts and writes | (b)'s base with a generation head. Diffusion only as an optional entrant | 2–8 GB | Mixed | Both slots | Two objectives in one model. Diffusion JSON weakness. Do this last |

Model facts:
[Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B),
[Qwen3 report, arXiv 2505.09388](https://arxiv.org/html/2505.09388v1),
[Gemma 4 E4B](https://huggingface.co/google/gemma-4-E4B-it),
[Granite 4.0 micro](https://huggingface.co/ibm-granite/granite-4.0-micro),
[Phi-4-mini, arXiv 2503.01743](https://arxiv.org/pdf/2503.01743),
[Arch-Router, arXiv 2506.16655](https://arxiv.org/html/2506.16655),
[RouteLLM](https://www.lmsys.org/blog/2024-07-01-routellm/).
BFCL numbers span v2–v4 and custom handlers, so they are not comparable
across vendors. Avoid as required bases:

- xLAM-2 1B/3B (CC-BY-NC).
- LFM2.5 (revenue-threshold license).
- Llama 3.2 and Arch-Router (custom licenses).

**Why (b) first:**

1. It is Peyton's "classifier that can do strings", aimed at the part of the
   bench that dominates v18: tool selection over a huge menu, span arguments
   and extractive memory.
2. Prefill-only scoring is cheap, needs no generation and is far easier to
   make **deterministic** (section 6.4). That attacks the dominant source of
   crown noise.
3. The plumbing exists: decision-lane pinning, metering, walls, outage codes
   and the priced-cost line.
4. Ditto gains a model it owns, replacing a third-party lane we pay for and
   cannot inspect.
5. Artifacts are small enough that hundreds of miners share one GPU through
   multi-LoRA serving.

## 5. Mechanism options

### M1. King of the Hill on a frozen harness (crown)

The miner uploads weights. Ditto serves them, and validators run the frozen
harness against them on ordinary scoring tickets. The crown moves by the
existing sequential paired dethrone.

- Precedents: SN9, SN37, Dippy, Affine era 1, Gradients.
- Strengths: maps one-to-one onto today's machinery. Miners may use any
  training method, including RL against the public generator, distillation
  and synthetic data. The winner is a finished, servable artifact.
- Weaknesses: winner-take-all stagnation (mitigated by the five-way curve and
  lineage releases), copy-and-tweak (section 6.3), and GPU hosting cost.

### M2. Continuous distributed training with checkpoints

Ditto owns one canonical checkpoint. Miners submit pseudo-gradients, LoRA
deltas or training batches. Validators accept a contribution when it improves
a held-out objective.

- Precedents: Templar, IOTA, Affine era 2, Psyche.
- Strengths: one compounding model with no copy race. Affine era 2 shows a
  cheap variant: miners submit **data** and the validator trains.
- Weaknesses:
  - The objective must be cheap and differentiable or per-step. A 350-case
    agentic run is neither: it costs dollars and minutes per evaluation, so
    per-step acceptance is too slow and too noisy (crown-pair sd ≈ 0.024–0.045
    per seed, per [title-duels.md](../title-duels.md)).
  - Validators would need GPUs to apply and test updates.
  - The attack history (norm attacks, freeriding, one bad update landing
    first) is long.
  - At ≤4B the scale argument for distributed training disappears.

### M3. Hybrid: crown plus lineage ("base of record")

M1 for scoring, plus three changes:

1. Each released champion becomes the next season's **base of record**.
   Fine-tuning it is legal and expected, not copying.
2. Ditto may periodically **merge or distill** the top-k released models into
   a reference model that serves Ditto's product lane. That is where the
   "continuous training" benefit comes from.
3. Optionally, an Affine-style **data lane**: miners submit decision training
   batches, and Ditto fine-tunes the base of record and gates on the bench.
   This would be a later track.

### Comparison

| | M1 KOTH | M2 Distributed | M3 Hybrid |
| --- | --- | --- | --- |
| Submission | LoRA or full weights plus manifest, private upload | Gradients, deltas or batches per round | M1, plus an optional data lane later |
| Size limit | Pinned base family, LoRA rank ≤ 64 or full ≤ 2B (v1) | Fixed by the canonical model | Same as M1 |
| Eval | Frozen harness × bench v18+, quorum 3, seasons | Held-out loss per step, plus a periodic bench | Same as M1. The bench gates merges |
| Anti-copy | Private store, tensor hash, delta similarity, behavior fingerprint, copy court | Assigned-data checks, norm caps | M1 plus a lineage alibi pool |
| Validator compute | Validators stay CPU. Ditto GPU pool | Validators need GPUs | Same as M1 |
| Emission | Track share × 50/20/13/10/7 | ∝ accepted contribution | M1, plus later a data-lane share |
| Fit for SN118 | High | Low at this scale and cost | **Highest. M1 now, M3 lineage from P3** |

## 6. Recommended v1: the Decide track

### 6.1 Contract

- **Frozen harness.** A Ditto-owned reference harness image, digest-pinned,
  versioned as `harness_version` and bound to each bench contract the way a
  generator pack version is. Candidate: the starter kit's v18 hybrid path
  (0.263 on seed 42 at full, $0.27) or a stronger harness built from released
  champion source (open question 4).
- **Miner slot: decision.** The chat slot stays the locked `gpt-oss-20b`, so
  every composite difference is attributable to the decision model.
- **Wire.** `ditto-decide-v1`, a strict subset of System One (`choice`,
  `noul`, `score`) plus `extract` in Mercury Decide's shape (fields → spans
  with offsets and `not_stated`).
- **Ditto owns the head. The miner owns only weights.** Ditto publishes the
  prompt renderer and read-out:
  - `choice`, `noul` and `score` are read from label-token logits after one
    prefill.
  - `extract` uses decoding constrained to substrings of `state`, so
    non-verbatim spans are impossible by construction.

  No miner code runs anywhere. That avoids `trust_remote_code`, sandboxing and
  source review.

### 6.2 Submission

- **Manifest `ditto-model.toml`**, inside an upload signed with the existing
  `ditto-upload-v2` hotkey signature. Fields:
  - `track = "decide"` and `slot`;
  - the pinned base (allowlist id plus SHA-256);
  - `kind = "lora" | "full"`;
  - per-file SHA-256 of the `safetensors`;
  - license attestation and training-data attestation (free text, shown to
    reviewers).
- **Size and format.**
  - LoRA rank ≤ 64 on allowlisted modules (v1).
  - Full weights within the base family at ≤ 2B parameters.
  - Tokenizer and config byte-identical to the base.
  - `safetensors` only: no pickle and no custom code.
- **Store.** A Platform-owned private bucket (AWS S3 proposal, or the existing
  Hippius private data plane), never a public HF repo. Optionally an on-chain
  commitment of the manifest digest for an independent timestamp. Timelock
  `set_reveal_commitment` follows Crusades.
- **Screening** replaces the Docker build:
  1. Parse and validate shapes and dtypes against the base.
  2. Reject NaN/Inf.
  3. Norm bounds per tensor.
  4. A load test in the serving image.
  5. A wire-conformance canary on public decision fixtures.

  Same fee (0.075 TAO), cooldowns and bans as today.

### 6.3 Anti-copy, layered

1. **Private until release.** Unreleased weights are never public and the
   endpoint is never exposed, so nobody can download or distill an unreleased
   model. Release follows the existing source-release policy. Once released,
   a model joins the alibi pool. That mirrors how the copy gate now counts
   released kings across bench eras (#3564).
2. **Exact duplicates.** A canonical hash over dtype-normalized tensors, so
   re-serializing changes nothing. The earliest signed upload wins (SN9's
   rule, without ε).
3. **Near duplicates (weights).** Compare the delta from base:
   - Full fine-tunes: per-layer relative Frobenius distance and cosine of ΔW.
   - LoRA: compare the effective ΔW = B·A, because raw A and B are not
     identifiable. Rotating A and B leaves ΔW unchanged.
   - Calibrate thresholds from independent fine-tunes on different seeds.
4. **Near duplicates (behavior).** Jensen–Shannon divergence of decision
   distributions on a private probe set. This catches neuron permutation,
   re-parameterization and a light fine-tune on top of a stolen model, which
   weight distance misses.
5. **Copy court.** A match on (3) or (4) against an unreleased model opens a
   `review_kind: copy` hold with the same alibi logic: public bases and
   released kings explain a match, unreleased-only content does not.
6. **Known leaks we accept.** Published transcripts show the champion's
   decision outcomes on its own seeds. That is no more than the answer key,
   which the public generator already gives everyone. Distilling Jev or
   Mercury Decide labels may break vendor terms. Ditto should neither
   encourage it nor try to police it; the attestation field records what the
   miner says they did.
7. **Overfitting the public generator is legal.** It is the expected strategy;
   current policy allows it. The defenses stay where they are now:
   the private generator slice (half the composite), seasons, and LongMemEval.
   In a model competition the generator competition becomes the adversarial
   data engine: generator miners find what the models miss, and model miners
   train on it.

### 6.4 Evaluation and inference hosting

- **Unchanged validators.** CPU-only, 4 vCPU / 32 GB / 160 GB disk
  ([VALIDATOR.md](../VALIDATOR.md#requirements)). They run the frozen harness
  image in the sandbox. The grant's decision pin becomes `miner:<manifest
  digest>`. The relay forwards to a Ditto inference pool instead of OpenRouter
  System One. The pin check, walls, metering and outage mapping are reused
  verbatim.
- **Inference pool** (AWS proposal): vLLM or SGLang with multi-LoRA serving
  on one pinned base per tier, on g6e.xlarge (L40S, $1.861/h) or g6.xlarge
  (L4, $0.805/h). Full-weight entrants are loaded per run batch. Instance
  prices: [Vantage](https://instances.vantage.sh/aws/ec2/g6e.xlarge).
- **Determinism.** Run the pool batch-invariant. vLLM ships
  `VLLM_BATCH_INVARIANT=1` (beta), including a fixed-order LoRA shrink
  reduction ([vLLM docs](https://docs.vllm.ai/en/latest/features/batch_invariance/)).
  Thinking Machines measured 80 distinct temperature-0 completions out of
  1,000 without it, and 1 with it, at about 1.6–2x latency
  ([blog](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/)).
  This matters more than it looks: per-seed noise between crown pairs is
  mostly run noise, not seed difficulty (cross-agent seed correlation is
  0.17 or lower, per [title-duels.md](../title-duels.md)). A deterministic
  decision slot shrinks the paired sd, so duels resolve in fewer seasons.
  The chat slot stays nondeterministic in v1, so the gain is partial; P0
  measures it.
- **Trust.** Validators trust Platform-served inference exactly as they trust
  the Platform-owned OpenRouter boundary today. Decision outputs are
  probability vectors, so a later validator option to recompute a sample on
  its own GPU and compare within tolerance (TOPLOC-style) is straightforward.
  That is the decentralization path; it is not needed for v1.
- **Unchanged scoring.**
  - Block-hash seeds, CRN, quorum of 3.
  - Dueling seasons with K = 3 seeds and the sequential paired dethrone.
  - Priced cost: decision input priced by **base size tier**, so smaller
    models earn a cost edge.
  - The LongMemEval confirmation lane. Decision access on confirmation
    sessions is required; the no-inference doc already flags that gap.
- **Outage voiding.** The pool is our infrastructure, so pool failures are
  no-fault (`model_relay_unavailable`). The canonical outage void with
  same-validator re-test (#3566) applies unchanged.

### 6.5 Emissions

- A new track, `model_decide`, beside `memory` and `generator`, folded under
  a new validator protocol. The track-share gate already refuses a share until
  the fleet reports the paying protocol.
- Share path, as the no-inference memo proposed for its track: **0 bps
  (shadow) → about 1,000 bps of the track pool → step up only on board depth
  (≥5 ranked distinct owners) and cost data.**
- Curve: reuse 50/20/13/10/7 and the crown rules. Add a **floor**: a model is
  paid only if it beats Ditto's own reference fine-tune on the same base by
  the dethrone margin. A stock or near-stock model earns nothing, and the
  track burns when nobody clears the floor.

### 6.6 What a miner does

1. Pull the public generator, frozen harness and decision renderer.
2. Generate unlimited `(state, question, gold)` training data:
   - Decision gold is derivable for most tool and memory cases (the grader
     knows the right tool, the record and the span).
   - Ditto can also publish a `decide-fixtures` exporter.
3. Run a LoRA or full fine-tune on one GPU.
4. Rehearse with `uv run ditto practice --decision-model <path>` (proposed)
   against a local vLLM.
5. Upload.

This is a much shorter loop than harness engineering, and it is the
"Bittensor is good at training models" lever Peyton pointed at.

## 7. Cost estimates (rough, to be replaced by P0 measurements)

Assumptions, all unmeasured on our workload:

- A v18 hybrid run sends 2–10M decision prefill tokens. The upper end is the
  full menu; the `grouped` menu reads about a fifth. The no-inference
  prototype used about 1.7M per 350 cases.
- A ≤2B model prefills about 20k tok/s on one L40S.
- Batch-invariant mode costs 1.6–2x.

| Item | Estimate |
| --- | --- |
| GPU time per scored run (decision slot, ≤2B) | 0.03–0.25 L40S-hours ≈ **$0.05–0.50** |
| Same tokens at Jev list price ($0.042/M) | $0.08–0.42 (today's cost for the lane) |
| Runs per day | Duels: 5 members × 3 seeds × 5 seasons = 75. New uploads: ~20 × quorum 3 = 60. **~135/day** |
| Pool size | ~5–35 GPU-hours/day. **1 always-on g6e.xlarge plus 1 for headroom and canaries** ≈ 2 × $1.861 × 730 h ≈ **$2.7k/month** on-demand. One instance plus spot burst ≈ $1.4k–1.8k/month |
| Storage | LoRA 50–500 MB, full ≤ 4 GB. 1,000 artifacts ≲ 1 TB ≈ $25/month S3 |
| v2 chat slot (≤4B, decode-heavy; v15 used 16.8M prompt + 1.36M completion tokens per run) | About 0.5–0.75 L40S-hours ≈ **$1–1.4 per run**. The same order as today's **$1.43 per v15 canonical run** ([no-inference-mode.md](../no-inference-mode.md)) |
| Miner cost per attempt | LoRA SFT ≤4B on 10k–100k examples: **$5–150** (Together at $0.48/M training tokens; Tev1-class runs about $17). Full FT ≤2B: about 2–6 H100-hours ≈ $10–40. Plus 0.075 TAO |
| M2 for comparison | Templar sized its 1.2B run at about one H100 per peer, and Covenant-72B at 8×B200 per peer. Validators would need GPUs. Not justified at ≤4B |

Pricing sources: [Vantage g6e](https://instances.vantage.sh/aws/ec2/g6e.xlarge),
[g6](https://instances.vantage.sh/aws/ec2/g6.xlarge),
[p5](https://instances.vantage.sh/aws/ec2/p5.48xlarge) ($55.04/h for 8×H100),
[Together fine-tuning pricing summary](https://www.cloudzero.com/blog/together-ai-pricing/),
[Tev1](https://github.com/togethercomputer/tev1).
Throughput anchors: Llama-3.1-8B on one H100 under vLLM reaches about 4–5k
output tok/s at 64–256 concurrency
([RunInfra](https://runinfra.ai/engines/vllm)). Prefill-only decision serving
is faster still. P0 replaces all of this with measured numbers.

## 8. Migration path

### Keep, change, add

| Keep as is | Change | Add |
| --- | --- | --- |
| Bench contracts, generator (public datagen half, private generator slice), block-hash seeds, CRN | The artifact: weights plus a manifest instead of a Docker build context | `harness_version`, frozen and digest-pinned per bench contract |
| Quorum of 3, dueling seasons, sequential paired dethrone, rank shares | Screening: tensor checks and a load test instead of image build and `/health` | Ditto inference pool (multi-LoRA, batch-invariant) behind the relay |
| Canonical outage voiding and re-test (#3566), no-fault relay codes | Copy evidence: tensor hash, ΔW similarity and behavior fingerprint instead of lexical and structural source fingerprints | `ditto-decide-v1` wire with `extract` |
| Copy holds and the released-king alibi pool (#3564), now with a lineage alibi | Daily top-five read: model card, attestation and probe behavior instead of source | Track `model_decide`, validator fold protocol, Backroom reads for every new knob |
| Generator competition, now the adversarial data engine | Priced cost by base-size tier | Miner tooling: fixture exporter, local decide server, `practice --decision-model` |
| LongMemEval confirmation | Decision lane must be open on confirmation sessions | Reference fine-tune per base, the pay floor |

The memory (harness) competition **keeps running**. The model track is
additive, like the generator track. Whether the harness track eventually
shrinks is a share decision, not a flag day.

### Phases

| Phase | What | Exit criterion |
| --- | --- | --- |
| **P0. Measure (≈2 weeks, research only)** | Hold the harness fixed (v18 hybrid). Swap decision models: Jev 1.14, Mercury Decide, open Kev-4B / Tev1, stock Qwen3.5-0.8B/2B/4B read-out, and a quick Ditto LoRA on generator-derived fixtures. 5 seeds at full. Also try the chat slot with Qwen3.5-4B / Gemma 4 E4B. Measure composite spread, per-seed paired sd with batch-invariant on and off, tokens, GPU time | **Go** if the spread across decent decision models is ≥ 3–5× the 0.007 margin, and a Ditto LoRA beats stock. Otherwise switch v1 to the chat slot or stop |
| **P1. Contract (weight 0)** | Manifest, upload and screening path. Wire and renderer published. Pool on AWS (Terraform plan/apply, protected). Grant pin `miner:<digest>`. Fixtures exporter and local rehearsal | Conformance vectors in CI. A Ditto reference model scores end to end on a canary |
| **P2. Shadow board (≥2 weeks)** | Open uploads, quorum scoring, seasons, copy gate v1, Backroom reads. 0 bps | ≥5 distinct owners ranked. Copy gate false-positive review. Crown stability. Cost per run within estimate |
| **P3. Carve a share** | ~1,000 bps of the track pool. Champion release, base-of-record lineage (M3) | Board depth holds. No unresolved copy rulings. Owner sign-off |
| **P4. Expand** | Chat-slot track (v2), then the one-model "Ditto Mercury" track (v3). Optional open architectures (diffusion) with Ditto-maintained serving adapters. Optional validator GPU spot-checks. Ditto's product decision lane served by the merged champion | Each step is its own contract version and its own shadow period |

## 9. Risks

- **The lever is too weak.** If the frozen harness caps the score, model
  differences vanish into harness error. P0 is the guard. A stronger frozen
  harness (open question 4) raises the ceiling.
- **The harness becomes the spec.** Models will overfit harness quirks. That
  is acceptable, because the harness is Ditto's product path, but changing it
  is a contract bump, like a bench version.
- **Platform-served inference concentrates trust and cost in Ditto.** This is
  the same trust as today's OpenRouter boundary. The spot-check path is the
  mitigation.
- **Calibration.** Open decision models' logprobs are uncalibrated (Tev1's own
  warning). The bench rewards outcomes, not calibration. If the product needs
  calibrated confidence, add a calibration term later.
- **Licensing.** Pin Apache-2.0 bases only, and record attestations for
  training data.
- **Precedent risk.** Most model-submission subnets were restructured. The
  difference here is a fresh-per-seed agentic bench, a private generator
  slice, cheap small-model evaluation and a five-way curve. Those address the
  overfit, cost and stagnation failures directly, but none of it is proven
  until P2.

## 10. Open questions for Peyton

1. **Mercury 3 or Mercury Decide?** Did you mean Inception's Mercury Decide
   (decision plus span extraction), or Mercury 3 (a generative diffusion LLM)?
   The recommendation assumes Decide.
2. **Which slot leads:** decision (recommended, pending P0) or chat?
3. **Base allowlist and size tiers for v1:** Qwen3.5-0.8B/2B only, or also 4B
   and Granite 4.0 1B? LoRA only, or LoRA plus full fine-tunes within family?
4. **Which frozen harness:** the starter kit's hybrid (0.263), or a
   Ditto-built reference from released champion source (higher ceiling,
   more work)?
5. **Hosting:** a Ditto pool on AWS (recommended), miner-hosted on Chutes
   with TEE (Affine era 1), or validator GPUs?
6. **Release policy for weights:** release champions on crown, after N days,
   or never? This sets how much lineage compounding we get.
7. **Emission share and timing** for `model_decide`. Should it ever draw from
   the memory track's share?
8. **Product intent:** is the winning model meant to replace Jev in Ditto's
   own product lane? If yes, latency and calibration targets belong in the
   contract from P1.
9. **Data lane (Affine era 2 style):** pay miners for training data that
   improves the base of record, as a later track?
10. **Distillation from closed decision models:** stay silent, discourage, or
    forbid?

## Sources

Repository:

- [MINER.md](../MINER.md)
- [VALIDATOR.md](../VALIDATOR.md)
- [title-duels.md](../title-duels.md)
- [no-inference-mode.md](../no-inference-mode.md)
- [benchmark-v18-rollout.md](../benchmark-v18-rollout.md)
- [router-compression-competition-v1.md](../router-compression-competition-v1.md)
- [seed-and-scoring.md](../../services/dittobench-api/docs/seed-and-scoring.md)
- [model-relay README](../../services/model-relay/README.md)
- [starter-kit README](../../miners/dittobench-starter-kit/README.md)
- [bench-versions.md](../../research/dittobench-datagen/docs/bench-versions.md)
- [dueling-bench.md](../../research/dittobench-datagen/docs/dueling-bench.md)
- [generator-competition.md](../../research/dittobench-datagen/docs/generator-competition.md)
- Live public status via `.agents/skills/subnet-status`, 2026-11-17

External (spot-checked for this memo):

- [Mercury Decide docs](https://docs.inceptionlabs.ai/capabilities/decisions)
- [OpenRouter: What is Jev](https://openrouter.ai/blog/insights/what-is-jev/)
- [REAX / SN92](https://reax.dev/)
- [Affine README](https://github.com/AffineFoundation/affine)
- [Tev1](https://github.com/togethercomputer/tev1)
- [Thinking Machines: Defeating Nondeterminism](https://thinkingmachines.ai/blog/defeating-nondeterminism-in-llm-inference/)
- [vLLM batch invariance](https://docs.vllm.ai/en/latest/features/batch_invariance/)

External (from the research pass, cited inline above; mostly vendor or
secondary sources for 2026 products): Inception, MarkTechPost, The Decoder,
AlphaSignal, the kartit.net ranking, Macrocosmos repositories and Substack
posts, Templar papers, Gradients docs and paper, Dippy docs, SimplyTAO, Prime
Intellect, Gensyn, Nous, the Hugging Face model cards and arXiv papers named
in each section, and Vantage instance pricing.
