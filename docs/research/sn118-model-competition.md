# SN118 model competition: a Ditto context-packet model that miners train

Status: **research memo, revision 3 (2026-11-18).** Nothing here changes a
live score, weight, setting, lease or piece of infrastructure.

- **Revision 1** (#3567) covered the SN118 mechanism. It is kept as Part II.
- **Revision 2** (#3568) added Part I, the context-packet model.
- **Revision 3** adds Part 0: Peyton's answers to the open calls, the
  private trace track, open weights at submit, and the approved P0 runbook.
  P0 spend is approved but **nothing has run**. Its AWS infrastructure goes
  through protected Terraform.

Revision 2 made this the central question:

> "Making our own decision model only makes sense if we hyperfocus this model
> on the things that Ditto needs. Our special sauce is creating the perfect
> context packet for a larger model. If the decision model could help in the
> retrieval process and the pruning process, because retrieval isn't enough:
> if you don't prune you're going to blow up the context window. Given these
> two outcomes I would want to know how we would design our own decision model
> in a way that would far outperform something general-purpose like Jev."
> — Peyton, 2026-11-17

## TL;DR (read-aloud length)

Jev is a general decision model. Ditto's edge is the context packet: the few
exact spans a big model needs, inside a token budget. So we build one small
Ditto model that ranks, prunes and extracts for that packet, and we train it
on the only thing that matters: how well the big model answers per context
token. DittoBench already knows the gold evidence for every case, so we can
label it, measure it and pay miners to improve it from day one. Start with an
autoregressive model of one to two billion parameters with decide and extract
heads. Diffusion is a later bet, not the first step. The proof is a P0 with the
same harness and the same big model: Jev against our model, comparing answer
quality at a fixed token budget.

Since 2026-11-18: the model goes into the product, its weights are public the
moment they are submitted, and it is scored privately on the team's own
traces through the real backend with production models.

## How to read this memo

- **Part 0** (revision 3) records Peyton's 2026-11-18 decisions and what they
  change:
  - open weights at submit;
  - the private trace track on the real backend;
  - production readers;
  - GLM-5.3-Flash as a v19 candidate;
  - where the CPM plugs into the product;
  - the approved P0 runbook.
- **Part I** (this part) answers Peyton's question:
  - the context-packet model (CPM): task, architecture, data, objective;
  - why it beats Jev;
  - how DittoBench measures it;
  - what each build path takes: (A) a Ditto diffusion LLM, (B) a Ditto
    Decide + Extract model, (C) an open-weight Ditto Extract for pruning;
  - the diffusion track in detail;
  - the product order and the P0 that proves it.
- **Part II** (revision 1, kept) is the SN118 mechanism: prior art, KOTH vs
  distributed training, submission format, anti-copy, hosting, cost and
  migration. Its "Decide track" is now the **Context track**: the same
  contract, with the miner slot being the CPM.

# Part 0. Peyton's decisions (revision 3, 2026-11-18)

Peyton answered the five open calls from §I.10 and §II.10 on 2026-11-18.
This part records the answers and works out what they change. Parts I and II
are kept; where a section is superseded, it says so and points here.

## R3.1 The decisions

| # | Call | Decision | What it changes |
| --- | --- | --- | --- |
| 1 | Is the CPM the product's packet builder? | **Yes.** "The primary problem with the subnet is that its outputs are not directly making it into the product." Ditto hosts the CPM privately (Ditto runs the inference) | The CPM is a product component from P1, not a benchmark lane. R3.6 shows where it plugs into the production retrieval path |
| 2 | Open weights? | **Yes, open.** Peyton cites the Bittensor precedent. Either every new king's checkpoint becomes public immediately, or every model is public at submit time | Replaces "private until release" (§II.6.2 *Store*, §II.6.3 layer 1). R3.2 recommends public at submit |
| 3 | Product traces? | **Yes, from a consented team cohort.** Team members donate their own traces, Peyton first. Miner models are scored in Ditto's private AWS infrastructure on the **real Go backend**, in an instance set up like a preview environment, not on the open-source Rust harness | Adds a private trace track (R3.3) beside the public DittoBench track |
| 4 | Which reader is the packet tuned for? | **Production readers.** Mostly the low-cost frontier models (Claude Haiku 5.5, GPT-6 Luna), plus some larger ones. `gpt-oss-20b` is being deprecated. Peyton also wants to consider **GLM Flash** as the DittoBench chat model: agent scores are already too high, so difficulty has to scale | Private track: production readers (R3.4). Public bench: a v19 candidate with GLM-5.3-Flash and harder cases (R3.5) |
| 5 | P0 spend (~$300–1,000) | **Approved.** AWS is the simplest choice (credits), with Lium as an option. Peyton expects the real data to matter enough to make this work better | R3.7 is the P0 runbook. Nothing has run yet |

## R3.2 Open weights, public at submit

**Recommendation: every submission is public at submit time.** That is
Peyton's second option. The precedent in §II.3 supports it, with one ordering
rule:

- **Public weights.** SN9 and SN37 models live on Hugging Face with an
  on-chain commit. Affine era 1 put models on HF and *encouraged* copying the
  leader, as long as the copy dominated it. Gradients open-sources its winners.
- **Commit before publish.** SN9 told miners to keep the HF repo private
  **until the on-chain commit landed**, because the first attack was copying an
  uncommitted model and committing it first. The commitment has to precede
  publication.
- **Paying for improvements.** SN9's winner-take-all curve "encouraged model
  hoarding". Coldint (SN29) forked partly to pay for publicly shared
  improvements. Our five-way curve plus public weights covers both.

This supersedes lesson 1 in §II.3 ("keep weights private until release").

The alternative, "public when crowned", keeps an in-flight challenger hidden
during its evaluation. But models that never win are never published, and the
lineage compounds more slowly. Public at submit is simpler and compounds
fastest.

**Mechanism.** It replaces §II.6.2 *Store* and §II.6.3 layer 1. Everything
else in §II.6.2–II.6.3 stands.

1. **Submit.** The upload is signed with the hotkey, as today. It carries the
   manifest plus the `safetensors` files, and nothing executable. The miner
   also writes the manifest digest as an **on-chain commitment**, which gives
   an independent timestamp that Platform cannot backdate.
2. **Publish.** Platform holds the upload privately until **both** of these
   are true:
   - the on-chain commitment has landed;
   - screening has accepted the upload (shape, NaN and norm checks, load
     test).

   Then it publishes immediately to a public mirror: a Ditto-run Hugging Face
   organization or public bucket. It goes up with the manifest, the commit
   block, the license and the training-data attestation. Nobody can see the
   weights before their priority is fixed, and miners don't need their own
   hosting.
3. **Priority and copies.** A submission whose weights or behavior fall within
   the copy threshold of an **earlier** public submission (§II.6.3 layers 2–4)
   is attributed to that earlier one. It earns only if it beats the original
   by the dethrone margin. This is SN9's ε rule. Fine-tuning on top of the king
   is legal and is the point: lineage compounds, but only through real gains.
4. **License.** Allowlisted bases must be Apache-2.0 or MIT. Submissions must
   carry a license that permits public release and Ditto's commercial use.
5. **What stays private:**
   - the trace set;
   - per-case private results;
   - any Ditto-internal model fine-tuned on traces.

   **Public weights are never trained on trace data**, because a model can
   memorize what it was trained on. Miners never see traces, so a miner's
   weights cannot contain them.

**What we accept.** Copiers can see a challenger while it is being scored. The
priority rule and the margin make an unimproved copy worthless. And open
weights are what Peyton asked for: anyone, Ditto included, can start from the
current best.

## R3.3 The private trace track

### R3.3.1 Shape

Two tracks score the same submitted weights:

| | Public track (validator-run) | Private track (Ditto-run) |
| --- | --- | --- |
| Cases | DittoBench v18/v19 packet replay (§I.7), generator plus private slice | Replayed turns from the consented team cohort |
| Harness | Frozen packet harness in the validator sandbox | **Real Go backend** at a pinned commit, in an ephemeral AWS eval lane |
| Reader | Bench chat model (GLM-5.3-Flash candidate, R3.5) | Production readers: Claude Haiku 5.5, GPT-6 Luna, plus a sample on a larger model |
| Grade | Deterministic DittoBench grade, Q@B | Agreement with a full-context reference answer, calibrated on team-labeled gold (R3.3.3) |
| Who can reproduce | Any validator | Ditto only; published as a signed score |

### R3.3.2 Data: the consented cohort

- **Who.** An allowlist of team accounts (company-domain accounts plus the
  cofounder who is not on that domain). The allowlist lives in private config,
  not in this public repository.
  - Each person's opt-in is recorded with a date before their data is
    exported.
  - Revoking consent removes that person from the next season's trace set and
    from any internal training split.
- **What.** The backend already logs every retrieval call (since 2026-05):
  - the query;
  - the candidate pool with per-candidate features;
  - the returned and hydrated memories.

  It also stores every turn's prompt and answer. One trace is:
  - the query;
  - a **point-in-time snapshot** of that user's memory store (only memories
    created before the turn);
  - the candidate pool;
  - the answer production gave;
  - any follow-up signal (a re-ask, a correction).
- **Only direct user↔Ditto conversations** (decided 2026-11-18). A team
  member's memories can contain other people's words, and their consent does
  not cover those people. So the export keeps only turns where the person
  wrote to Ditto and Ditto answered. It excludes:
  - group, shared and company chats;
  - chat-platform messages (Slack, Telegram and similar);
  - connected-app content (email, documents, calendar, MCP);
  - anything another person wrote;
  - agent, workflow and system messages.

  Each conversation row records where the message came from, so the filter
  is exact. The consent template is heyditto-stack
  `docs/engineering/trace-donation-consent.md`, and completed forms are
  recorded on a private Jira issue.
- **Handling.**
  - Exported into an encrypted S3 bucket in Ditto's AWS account: KMS, public
    access blocked, access logged, readable only by the eval role.
  - Production encrypts conversation content at rest, so a plain SQL read
    returns ciphertext. The export has to go through the backend's own
    decrypting export path, run for each consenting account.
  - Never in a repository, PR, W&B or Backroom.
  - A deterministic scrubber pseudonymizes emails, phone numbers and addresses
    consistently across a trace.
- **Splits.**
  - A per-season **private eval pool**, which nothing is ever trained on.
  - An optional **internal train split** for Ditto's own fine-tune.
  - A frozen **P0 slice**.

### R3.3.3 Grading real turns without a gold answer

Real turns have no `expected_answer`. Three layers:

1. **Reference answer, once per season.**
   - A frontier model receives the trace's **entire** candidate pool,
     uncapped, and answers.
   - That answer is the reference.
   - The private metric asks how close the reader's answer from a B-token
     packet comes to the reference. That is Q@B, the same "quality per context
     token" objective as §I.5.
2. **A pinned judge.**
   - A low-cost model grades agreement against a fixed rubric.
   - The judge model, prompt and seed are published as a season commitment.
   - A stronger judge re-grades a sample, to measure judge noise.
3. **Team-labeled gold.**
   - Each team member labels about 20–50 of their own traces: which memories
     were needed, and whether the reference is right.
   - That calibrates the judge and gives an exact needed-memory recall@B,
     like the generator gold in §I.7.

### R3.3.4 The eval lane: real backend, preview-style, in AWS

The backend already has a DittoBench harness. It restores pre-embedded
fixtures into a private Postgres and runs the real retrieval and chat path;
that is how Ditto measures LongMemEval today. The private track extends it
with a **trace-replay adapter**. The lane is modeled on the backend's
per-PR preview environments, with these parts:

- **Backend.** Pinned to a commit per season. Its CPM endpoint points at the
  lane's model server.
- **Postgres.** Restored from the season's fixture snapshot.
- **Model server.** vLLM, batch-invariant (§II.6.4), on one g6e.xlarge (L40S,
  $1.861/h) loaded with the submission's weights.
- **Network.** The lane can reach only Ditto's inference router, for reader
  calls. Nothing else leaves it.
- **Safety.** A submission is inert `safetensors`, so **no miner code runs**.
  That is a smaller trust surface than today's harness sandbox.

### R3.3.5 Trust and scoring

Validators cannot reproduce the private track. Platform publishes a signed
private score per submission, and a new validator protocol folds it in. This
makes part of SN118 owner-attested. The mitigations:

- **Season commitments** published before submissions open:
  - the hash of the trace set;
  - the reference model;
  - the judge configuration;
  - the backend commit.
- **Deterministic CPM serving.** Re-scoring a submission gives the same
  packets.
- **Per-case score vectors under opaque case ids**, published after the
  season closes, so anyone can check the aggregate.
- **Leakage limits.** No per-case private results during a season. A
  submission learns one aggregate number, so probing the private set costs a
  fee and a cooldown per bit.
- **A public half the validators still run**, so SN118 is never wholly
  owner-scored.

**Blend.** The private track starts as **shadow (0%) for one season**. That
season measures how well public and private scores agree:

- **High agreement:** the public bench is a valid proxy, and the private share
  can stay small.
- **Low agreement:** the public bench is mis-specified. The private share
  rises toward 50%, and the generator competition is aimed at the gap.

The private track is also the strongest overfit defense we have. Policy allows
overfitting the public generator, so we fight it with bench design, and real
traces are the one case set nobody can generate.

### R3.3.6 Cost per submission (estimate)

Prices are list prices:

- **Claude Haiku 5.5:** $0.10 in / $0.50 out per M (prompts ≤100K).
- **GPT-6 Luna:** $0.10 / $0.50, from third-party pages, not OpenAI's own.

At 500 cases × 2 readers × 3 replays = 3,000 reader calls of about 5K tokens in
and 400 out:

| Item | Estimate |
| --- | --- |
| Reader calls | ≈ $2 |
| Judge (low-cost model, ~3K in / 150 out each) | ≈ $1–2. A Sonnet-class judge would be ≈ $20 |
| Larger-reader sample (10%, Sonnet-class) | ≈ $2 |
| GPU lane (batched; boot, load and serve) | ≈ $1–2 |
| **Per submission** | **≈ $5–10** |
| Reference answers, once per season (500 × ~30K tokens on a frontier model) | ≈ $50–100 |

## R3.4 Readers

- **Private track.** Score each packet on Claude Haiku 5.5 and GPT-6 Luna,
  with a 10% sample on a larger model. The CPM objective (§I.5) is unchanged.
  Q is averaged over readers, so a packet must work for more than one model.
  This is the transfer check §I.10 call 4 asked for, built into the score.
- **Public track.** Moves off `gpt-oss-20b` with the next bench version
  (R3.5).
- **Product.** The packet the CPM builds is the packet production sends. There
  is no separate "benchmark packet".

## R3.5 GLM-5.3-Flash as the DittoBench chat model (v19 candidate)

**Facts.** All are third-party write-ups; none is Z.ai's own model card.

| Field | Value |
| --- | --- |
| Model | GLM-5.3-Flash, released 2026-08-26 |
| Weights | MIT open weights |
| Size | 320B total / 18B active MoE |
| Context | 1M tokens |
| Price, Z.ai and most OpenRouter providers | $0.15 / $0.50 per M |
| Price, cheapest OpenRouter listing | $0.04 / $0.08 per M. Listing prices moved between snapshots |
| API features | Tools, `tool_choice` and reasoning effort |

**What the swap does and doesn't do.**

- **A stronger reader raises scores on today's cases; it does not make the
  bench harder.** If scores are already too high, the swap has to ship with
  harder cases in the same bench version. Peyton's thesis points to the right
  difficulty lever: **grow the haystack and shrink the budget B.**
  - Bigger memory stores and catalogs.
  - More supersession and temporal traps.
  - A tighter token budget.

  That is harder for harness agents, and it is exactly where a better packet
  pays.
- **Cost.**
  - At $0.15 / $0.50, a run costs roughly 2–2.5× what it does on
    `gpt-oss-20b`. The starter hybrid would go from $0.27 to about
    $0.55–0.70, and heavy agents would cross the $1.00 v18 run budget.
  - So v19 either raises the budget or pins a cheaper provider. One
    provider is an availability risk.
  - Reasoning tokens add output cost, so pin the reasoning effort.
- **Determinism.** It is an API model, so pin the provider and the effort.
  Self-hosting needs about 330 GB for FP8 weights (a multi-GPU node), so it is
  not worth doing for the bench.
- **Process.** This is a bench-version bump across every layer
  (`.agents/skills/ditto-subnet-bench-version-bump`), with a matching generator
  pack. P0-G (R3.7) sizes the score lift and the cost before anyone writes the
  contract.

## R3.6 Where the CPM plugs into the product

Production retrieval today runs in five steps:

1. vector candidates;
2. learned fusion weights (a small MLP);
3. a cross-encoder rerank;
4. an **LLM rerank** on a frontier "Luna" model, which returns an order plus a
   **sufficiency** flag;
5. when the candidates are insufficient, one more retrieval.

The CPM replaces steps 3 and 4 and adds span pruning:

| Today | With the CPM |
| --- | --- |
| Cross-encoder rerank + LLM rerank (a full API call per turn) | One CPM forward pass on Ditto's GPU (p95 ≤400 ms, §I.3) |
| Sufficiency flag from the LLM rerank | The CPM's decide head (`sufficient`, plus `absent` for abstention) |
| Whole memories in the prompt | Spans packed under budget B (§I.2) |
| Retrieval logs used to retrain the fusion MLP | The same logs become trace fixtures (R3.3.2) |

So "today's production path" is the P0-T baseline arm. Beating it is the bar
for shipping, and that is a stronger bar than beating Jev.

## R3.7 P0 runbook (approved, not started)

**Infrastructure** (AWS, Ditto account, credits):

- **Provisioned through protected Terraform plan/apply** in the owning infra
  repo, with a teardown-date tag. Nothing is created by hand.
- **GPU.** One g6e.xlarge (L40S 48 GB, $1.861/h), for CPM training and the
  model server. Spot instances where training can checkpoint.
- **Eval lane.** A small CPU instance for the backend and Postgres.
- **Storage.** The encrypted trace bucket.
- **P0-D.** A larger GPU node, only for the diffusion head-to-head and only if
  that arm runs.

**Experiments:**

| Experiment | Question | Arms | Win |
| --- | --- | --- | --- |
| **P0-A** (public; §I.10) | CPM vs Jev on DittoBench | As in §I.10, plus a **GLM-5.3-Flash reader** arm beside `gpt-oss-20b` | As in §I.10 |
| **P0-T** (private; new) | Does a CPM trained **only on public synthetic data** beat production on real team traces, and how much do real traces add? | 1. Today's production path (cross-encoder + LLM rerank); 2. Jev; 3. CPM v0 (public data only); 4. CPM v0 + internal trace fine-tune (train split; stays private). Readers: Haiku 5.5 and GPT-6 Luna, plus a 10% larger-reader sample | **Arm 3 ≥ arm 1 at ≤60% of the tokens, or +0.02 Q at equal tokens.** Team-labeled needed-memory recall@B ≥ 0.9. p95 latency ≤ production's rerank call. **The gap between arms 3 and 4 is the key number**: small means the public bench is a valid proxy for miners; large means the private track must carry weight |
| **P0-G** (public; new) | How much does GLM-5.3-Flash lift v18 scores, and what does a run cost? | Starter hybrid plus released king sources, 3 seeds each, `gpt-oss-20b` vs GLM-5.3-Flash | Measures the lift (sets how much harder v19 must be) and the cost per run against the $1.00 budget |
| P0-S, P0-D | §I.10, §I.9.5 | Unchanged | Unchanged |

**Budget.**

- GPU: about $300–700.
- Inference: about $50–150. That covers the season references (≈$65), P0-T
  readers and judge (≈$20), P0-A runs (≈$30) and P0-G runs (≈$30).
- Total: within the approved $1,000, on AWS credits.

**Before anything runs:**

1. **Opt-ins.** A dated opt-in from each person in the cohort.
2. **Third-party rule.** Confirm the P0 rule on connector content (R3.3.2).
3. **Access.** An AWS permission set that can apply the P0 Terraform, and
   read-only production database access for the export.
4. **Private runbook.** The cohort allowlist, export query and bucket names go
   in a private runbook, not in this public memo.

## R3.8 What is still open

1. ~~Third-party content in team traces.~~ **Resolved 2026-11-18:** only
   direct user↔Ditto conversations (R3.3.2).
2. **Private share after the shadow season.** Is the ceiling 50%, and what
   public–private agreement keeps it lower?
3. **Release mirror.** A Ditto Hugging Face organization, or a public bucket?
4. **v19 contents.** GLM-5.3-Flash together with the haystack and budget
   increase, after P0-G.
5. **Lium vs AWS for GPUs.** The P0 default is AWS, for the credits.

# Part I. The Ditto context-packet model

## I.1 Answer in one paragraph

The win does not come from a bigger or cleverer general classifier. It comes
from changing the task, along three lines:

1. **What one call does.** Jev answers isolated typed questions. A Ditto CPM
   looks at the whole candidate set at once (memories, subjects, tool output,
   history), against Ditto's memory schema and a token budget.
2. **What it emits.** Jev returns labels. A CPM returns a packet plan: scores,
   keep/drop, exact spans, a per-source budget, and "retrieve more" or
   "absent" signals.
3. **What it is trained on.** Jev is trained for label accuracy. A CPM is
   trained for the downstream big model's graded answer quality per context
   token.

DittoBench is the unfair advantage. Every generated case already carries:

- the gold evidence records (`v10_evidence_pair_ids`);
- the expected answer and accepted variants;
- hard negatives (`distractor_answers`, the forbidden answer);
- the coined needle that served tool output embeds;
- the correct tools and argument claims.

These are free, unlimited and privacy-safe labels that a general vendor model
never sees.

## I.2 Task definition

**Pipeline.** The CPM is stage 1 of three. Stages 0 and 2 are deterministic
Ditto code.

| Stage | Owner | Job |
| --- | --- | --- |
| 0. Recall | Ditto code | BM25 plus embeddings plus subject-graph expansion. Returns N ≈ 100–300 candidates with ids (memory pairs, subjects, tool-menu entries, tool-output chunks, history turns). Cheap and high recall |
| **1. Packet model (CPM)** | **the model** | Joint scoring, pruning and extraction against a budget. One forward pass per window |
| 2. Packer | Ditto code | Renders the plan into the big model's prompt: order, dedupe, verbatim spans with source ids, budget enforcement |

**Inputs** (one `packet` request):

| Field | Content | Ditto source |
| --- | --- | --- |
| `query` | The user turn, the time anchor ("as of"), the system-prompt digest | `/run` `user_input`, `system_prompt` |
| `user_state` | Stable profile facts, stored defaults (e.g. reasoning effort), linked connectors, current date | user settings, subject graph |
| `space_state` | Active space or scope, its pinned subjects, recent activity | the space |
| `candidates[]` | `{id, kind, text, meta}` where kind ∈ `pair` (prompt/response, `session_id`, `timestamp`), `subject` (`subject_text`, `description_text`, linked pair ids), `tool` (name, description), `tool_output` (chunk of a served result), `turn` (history) | `/seed` pairs, subjects, links; tool menu; tool results; conversation |
| `budget` | Target packet tokens B, optional per-source caps | cost policy, big-model window |

**Outputs** (the packet plan):

| Output | Type | Used for |
| --- | --- | --- |
| `relevance[id]` | calibrated probability | ranking, thresholding |
| `keep[id]` | keep / drop / keep-as-pointer (id only, no text) | pruning |
| `spans[id][]` | exact substrings with char offsets, a field tag (`answer_value`, `support`, `temporal_anchor`, `supersedes`, `arg_candidate`) and a confidence | span-level pruning; argument filling; the `answer` slot |
| `allocation` | tokens per source (memories, tools, history) summing to ≤ B | budget packing |
| `route` | recall / act / remember / chat, plus restraint (no_call, clarify, decline) | harness control (replaces the Jev route questions) |
| `tool_shortlist` | top-k tool ids with scores | tool menu pruning (the v18 1,400–2,000-tool problem) |
| `signals` | `need_more_retrieval` (with an expansion: a query string as a span of the input, or subject ids), `conflict` (ids that disagree, and which is newer), `absent` (probability the answer is not in context) | second-round retrieval; abstention; supersession |

**Wire.** `ditto-packet-v1`, a superset of the System One body (`choice`,
`noul`, `score`) plus Mercury-style `extract`, plus a `packet` question type
carrying the candidates and budget. Spans are guaranteed verbatim by
construction: the head tags input tokens and never generates text. That
matches Inception's own guarantee for Extract, "every span is an exact
substring of your input"
([Extract docs](https://docs.inceptionlabs.ai/capabilities/extract),
spot-checked). It also stays inside DittoBench's `answer_in_prompt` exemption,
because values come from the request, `/seed` records and served tool results.

## I.3 Architecture options and latency targets

| Option | Shape | Strengths | Weaknesses |
| --- | --- | --- | --- |
| **A. Cross-encoder reranker plus span tagger** | ModernBERT or Ettin encoder (150M–1B) scoring `(query, candidate)` pairs, and a GLiNER2- or LLMLingua-2-style token tagger | Proven: LLMLingua-2 compresses by token classification with a bidirectional encoder, 2–5x compression at 1.6–2.9x end-to-end speedup. Provence frames pruning as sequence labeling unified with reranking. Tiny, CPU-capable, deterministic | Pairwise: no listwise view of redundancy or conflicts. Two models. Short windows (8K for Ettin). Weak at route and tool decisions |
| **B. Small AR decide + extract** | Qwen3.5-0.8B/2B with one prefill over `query + candidates`, label-logit decisions, and a token-tagging head on hidden states | Listwise. One model for all outputs. 262K context. The base open decision models already use (Tev1, Kev). Batch-invariant serving exists | Causal attention tags each token with left context only (mitigate: query and criteria first, or a bidirectional fine-tune). Larger than A |
| **C. Diffusion / bidirectional extract** | A block-diffusion LM (LLaDA2.x-mini, SDAR-4B, Nemotron-Labs-Diffusion-3B) used as a bidirectional encoder for tagging, with generation available later | Full bidirectional context inside a block. Same weights could later generate | Small open bases are 16B resident (LLaDA2 mini) or custom-licensed. Worse published calibration. No batch-invariant serving. Needs `trust_remote_code` (see I.9) |
| **D. Multi-head on one backbone (recommended)** | B's backbone with three heads sharing one prefill: listwise relevance (logit at each candidate's marker token), token BIO/keep tagging per field, decision logits | One pass, one artifact, one determinism story. Every output trained jointly against the same end-to-end reward | Needs custom serving for the heads. Ditto owns this; miners only supply weights |

**Latency targets** (stage 1, per packet request, one L4/L40S-class GPU,
≤16K candidate tokens):

- p50 ≤ 150 ms, p95 ≤ 400 ms.
- Total pre-model overhead (stages 0–2) ≤ 15% of the big model's
  time-to-first-token.

For reference, Jev is about 70–500 ms per call (vendor claim), and the v18
hybrid harness makes many Jev calls per case: menu chunks, note ranking, one
per argument, next-tool. A single CPM pass that replaces most of them is a
latency win even if one call is slower.

**Long inputs.** Window the candidates at 8–16K tokens, repeat the query prefix
(prefix KV cache), score listwise within each window, and merge by calibrated
score. Tool outputs over 16K are chunked, and the needle-bearing chunk is the
training target.

## I.4 Training data

| Source | Labels | Scale and cost | Privacy |
| --- | --- | --- | --- |
| **1. DittoBench generator (public datagen)** | `v10_evidence_pair_ids` → relevant pairs. `expected_answer` / `accept_any` located inside evidence → `answer_value` spans. `distractor_answers` and the forbidden answer → hard negative spans. Fixture `needle` in served tool output → tool-output spans. Correct tool and `required_arg_claims` → shortlist and `arg_candidate` spans. Absence families → `absent`. Supersession and point-in-time families → `supersedes`, `temporal_anchor`, `conflict` | Unlimited, CPU-only, deterministic per seed | None: synthetic |
| **2. Downstream ablation labels** | Run the locked big model on packets with candidates or spans dropped. The label is Δ grader score (leave-one-out, or sampled subsets) | ~10k–100k episodes. At ~4K prompt tokens and $0.07/M for `gpt-oss-20b`, 100k episodes ≈ 400M tokens ≈ **$28** | None |
| **3. Teacher labels** | An open-weight large model annotates relevance and spans on synthetic and public data | Moderate | Only open-weight teachers whose license allows distillation. Do not train on closed-vendor outputs (Jev, Mercury); that is a ToS risk |
| **4. Public data** | LongMemEval-S (already the confirmation lane), MS MARCO / BEIR for reranker warm-up, the LLMLingua-2 MeetingBank compression set | Large | Check each license |
| **5. Ditto product traces** | Real queries, retrieved memories, tool outputs, big-model answers, user feedback | The real distribution | **Opt-in only: the consented team cohort (decided 2026-11-18, R3.3).** Scrubbed and kept inside Ditto infrastructure. Never shared with miners or validators. Used for the SN118 private trace track (scores only leave Ditto) and Ditto's internal fine-tune. Public weights are never trained on it. Miners train on synthetic and public data only |

The SN118 split matters. Miners train on sources 1–4. The crown is decided on
the private generator slice and fresh block-hash seeds, and Ditto's product
eval on source 5 never leaves Ditto.

## I.5 Objective: answer quality per context token

Peyton's framing is the objective. We optimize

  **J = E[ Q(big-model answer | packet) ] − λ · tokens(packet)**

or, equivalently, Q at a fixed budget B. Q is the DittoBench deterministic case
grade: no judge, reproducible.

| Stage | Loss | Signal |
| --- | --- | --- |
| S1. Supervised | Listwise softmax cross-entropy over candidates, token BIO cross-entropy per field, decision cross-entropy | Generator gold (source 1) |
| S2. Budgeted | The same losses under randomly sampled budgets B. Recall of `answer_value` spans weighted highest, then `temporal_anchor` and `supersedes`, then `support` | Teaches allocation and graceful degradation |
| S3. End-to-end | Policy gradient (GRPO-style) over sampled keep/span decisions, with the frozen locked big model as the environment and reward = case grade − λ·tokens | Learns what the big model needs (e.g. keep the "as of" anchor), not what a labeler thinks is relevant |

Report standalone classification accuracy only as a diagnostic. The model ships
on the metrics in I.7.

## I.6 Why a Ditto CPM should beat Jev

| | Jev (general System One) | Ditto CPM |
| --- | --- | --- |
| Unit of work | One typed question, ≤255 options, answered in isolation | The whole candidate set, listwise: sees redundancy, conflicts and complementary facts (owner → email multi-hop) |
| Strings | None. `box` is visual only. The harness fakes spans with clause-then-span `choice` | Native verbatim spans with offsets |
| Schema | Generic state | Ditto pairs, sessions, timestamps, subjects and links. Supersession and point-in-time are first-class labels |
| Budget | No notion of tokens | Allocation is an output, trained against a budget |
| Training target | Label accuracy on general decisions | The big model's graded answer per token (S3) |
| Data | General | Unlimited gold from the generator, plus an adversarial generator competition that keeps producing the cases agents miss |
| Calls per case | Many (menu chunks, notes, each argument, next tool) | One per window (plus an optional retrieve-more round) |
| Cost and control | $0.042/M input, vendor-pinned, vendor drift risk, user memories sent to a third party | Owned weights, deterministic serving, no per-token fee, memories stay in Ditto |

**Honest caveat.** On out-of-domain general decisions Jev will likely stay
stronger. Keep Jev as the product fallback until the CPM wins P0 and a
production shadow.

## I.7 DittoBench metrics for context-packet quality, and how miners compete

**Metrics** (all computable from existing generator metadata and the
deterministic grader):

| Metric | Definition | Role |
| --- | --- | --- |
| **Q@B** | Composite (and memory/tool means) with the packet hard-capped at B ∈ {1K, 2K, 4K, 8K} tokens, big model and harness fixed | Headline. Also the area under the Q-vs-B curve |
| **Needed-fact recall@B** | Share of gold evidence (answer-bearing spans plus required supporting records) present in the packet | Diagnostic and cheap pre-screen |
| **Distractor and forbidden inclusion** | Share of packets that contain a `distractor_answers` value or the forbidden answer | Precision proxy |
| **Tokens saved** | Packet tokens vs (a) today's hybrid packet (8 notes plus 3–6 tool schemas) and (b) the full-context baseline. Also the priced-cost factor | Cost |
| **Latency** | Stage-1 p50/p95 per call, and added end-to-end time per case | Gate |
| **Absent and conflict calibration** | Brier of `absent` on the absence families; precision of `need_more_retrieval` | Diagnostic |

**Two run modes:**

1. **Packet replay (offline, cheap).** Stage-0 candidate sets are recorded per
   seed with gold labels, and the CPM is scored on recall@B, inclusion and
   calibration with no big-model calls. This is the screening gate and the
   miner's local loop (`uv run ditto practice --packet-model <path>`,
   proposed).
2. **Full bench (the crown).** Frozen packet harness, frozen big model
   (`gpt-oss-20b`), budget pinned per contract. Scored by the bench composite
   under the existing priced-cost contract, so token savings already count.
   Quorum of 3, dueling seasons, sequential paired dethrone, as in Part II.

**How SN118 miners compete.** The Context track (Part II §6, with the slot
renamed):

- Miners submit weights for stage 1 only: a pinned backbone family, LoRA or
  full fine-tune.
- Ditto owns stage 0, stage 2, the heads' read-out code and the big model.
- Anti-copy, private-until-release, hosting and emissions are as in Part II.

Three forces keep it honest:

- Generator miners attack the CPM: buried needles in long tool output,
  near-duplicate distractors, corrections arriving in later waves, multi-hop
  evidence.
- The private generator slice decides the crown.
- Champion releases compound through lineage.

The champion can also be offered to **harness miners** as a lane, like the
decision lane today, and to **Ditto product** behind the router. Its existing
tool-result-compression and memory-recall levers
([router-compression-competition-v1.md](../router-compression-competition-v1.md))
are exactly where a CPM plugs in.

## I.8 What each build path would take

| | (A) Ditto diffusion LLM (Mercury 3-like, generative) | (B) Ditto Decide + Extract (AR or diffusion) | (C) Open-weight Ditto Extract / CPM for pruning |
| --- | --- | --- | --- |
| Base | Fine-tune LLaDA2.1-mini (Apache-2.0, 16B / ~1.4B active), SDAR-4B (MIT) or Nemotron-Labs-Diffusion-3B (NVIDIA license). Converting our own AR base is an option (SDAR-style, ~50B tokens) | Qwen3.5-0.8B/2B (Apache-2.0) with label-logit and tag heads. Or an encoder (ModernBERT / Ettin / GLiNER2) | Same backbone as B, plus listwise relevance and budget heads (option D) |
| Data | Generator SFT data, distillation from the locked open-weight `gpt-oss-20b` on generator cases, public instruction and tool data | Generator gold (I.4 source 1), teacher labels from open models | B's data plus downstream ablation labels and S3 rewards (I.4 sources 1–3) |
| Training compute and cost | LoRA SFT: a few to tens of A100-hours (extrapolated; no primary source gives a dollar figure), so **~$100–1,000 per run**. Own AR→block-diffusion conversion at SDAR scale: thousands of GPU-hours, **~$10k–50k** (estimate). Dream-scale (580B tokens) **~$90k**. From scratch (LLaDA 8B) **~$675k** | LoRA ≤4B: **$5–150 per run**. Full FT ≤2B: **$10–40** | S1–S2 as B. The S3 reward stage needs ~400M big-model tokens ≈ **$30**, plus a few GPU-days ≈ **$100–500** |
| Serving | SGLang dLLM framework (LLaDA2.x upstream), dInfer, vLLM for DiffusionGemma only. `trust_remote_code` bases | vLLM or SGLang, prefill-only, batch-invariant mode (beta) | Same as B, plus a small custom head server Ditto owns |
| Determinism for validators | **Weak.** Pin the sampler (I.9.3). Algorithmically deterministic at temperature 0 with low-confidence remasking and fixed steps, but not bitwise across GPUs, batch sizes or kernels. One 2026 study calls dLLM nondeterminism "pervasive". Compare within a tolerance | **Strong.** One forward pass, argmax read-out. Batch-invariant kernels give bitwise repeats on fixed hardware | **Strong**, as B |
| Team and time | 2–3 ML engineers. **3–6 months** to a competitive fine-tune, 6–12 months for our own conversion | 1–2 engineers. **6–10 weeks** to v0 | 2 engineers. **8–12 weeks** to v1, with P0 inside the first 3 weeks |
| Product value | Fast generation for the chat slot. Unproven on our JSON and tool path | Replaces Jev calls | Directly cuts big-model tokens on every Ditto turn. That is the special sauce |

## I.9 The diffusion track

Peyton asked: "we should train a diffusion model?" This section answers it.

### I.9.1 Open diffusion LLM bases (fine-tune, don't pretrain)

| Model | Params (total / active) | Kind | Context | License | Tools / JSON evidence |
| --- | --- | --- | --- | --- | --- |
| **LLaDA2.0-mini** (2025-12) | 16B / 1.4B (MoE) | Block diffusion, converted from AR Ling-mini-2.0 | 32,768 | Apache-2.0 | "Supports tool calling". BFCL_Live 70.90 vs Qwen3-8B 70.08 (vendor). [card](https://huggingface.co/inclusionAI/LLaDA2.0-mini) (spot-checked), [paper](https://arxiv.org/html/2512.15745v2) |
| **LLaDA2.1-mini** (2026-02) | 16B | Adds token editing | 32,768 | Apache-2.0 | BFCL v3 73.61 in quality mode (vendor). [card](https://huggingface.co/inclusionAI/LLaDA2.1-mini) |
| LLaDA2.2-flash (2026-07) | 100B | Edit plus agentic RL | 128K | Apache-2.0 | BFCL-V4 60.78 vs 66.81 for the AR sibling. No mini. [card](https://huggingface.co/inclusionAI/LLaDA2.2-flash) |
| **SDAR-1.7B/4B/8B** (2025-07) | 4B dense option | Block diffusion converted from Qwen3 (50B continued-pretraining + 4B SFT tokens) | unverified | MIT | 1.7B "on par with" its AR SFT counterpart. [repo](https://github.com/JetAstra/SDAR) |
| TraDo-4B/8B (2025-09) | 4B / 8B | SDAR lineage (unverified), TraceRL | — | weights unverified | Math and code. [repo](https://github.com/Gen-Verse/dLLM-RL) |
| **Nemotron-Labs-Diffusion-3B** (2026-05) | ~4B dense (the Hub lists 4B) | **Tri-mode: AR, diffusion and self-speculation in the same weights** | not stated | NVIDIA Nemotron Open Model License (custom, commercial use allowed) | Not documented. [card](https://huggingface.co/nvidia/Nemotron-Labs-Diffusion-3B) (spot-checked) |
| DiffusionGemma-26B-A4B-it (2026-06) | 25.2B / 3.8B | Gemma 4 converted to block diffusion | 256K | Tagged Apache-2.0, but links to the Gemma 4 license page: check | Native tools. Tau2 56.2 vs 68.2 for its AR parent. Stochastic sampler. [report](https://arxiv.org/html/2608.00146v1), [vLLM](https://vllm.ai/blog/2026-06-10-diffusion-gemma) |
| LLaDA-8B / LLaDA 1.5 | 8B dense | Masked diffusion from scratch | 4,096 | MIT | BFCL-v3 19.4. [card](https://huggingface.co/GSAI-ML/LLaDA-8B-Instruct) |
| LLaDA-MoE-7B-A1B | 7B / 1.4B | Masked diffusion | **4,096** | Apache-2.0 | Too short for our contexts. [card](https://huggingface.co/inclusionAI/LLaDA-MoE-7B-A1B-Instruct) |
| Dream-v0 7B | 7.6B | From Qwen2.5-7B | **2,048** | Apache-2.0 | BFCL-v3 13.6. [repo](https://github.com/DreamLM/Dream) |
| DiffuCoder-7B | 7.6B | From Qwen2.5-Coder | — | **Research-only / non-commercial**: exclude | Code. [card](https://huggingface.co/apple/DiffuCoder-7B-cpGRPO) |
| Open-dCoder 0.5B | 0.6B | Masked diffusion | — | Apache-2.0 | Code only, no numbers. [card](https://huggingface.co/fredzzp/open-dcoder-0.5B) |
| Seed Diffusion, Gemini Diffusion, Mercury 3 | — | — | — | **Closed** | — |

Credible bases for a ≤4B-active track are **LLaDA2.1-mini** (best license and
tool evidence, but 16B to hold in memory), **SDAR-4B** (MIT, dense) and
**Nemotron-Labs-Diffusion-3B**. Nemotron's built-in AR mode is a free
same-weights control for the head-to-head. Its custom license and missing SFT
code are the costs.

### I.9.2 Fine-tune recipe and cost

- **Objective.** Noise only the response tokens and keep the prompt clean.
  Masked cross-entropy is weighted by 1/p_mask and normalized by answer length
  ([LLaDA guidelines](https://raw.githubusercontent.com/ML-GSAI/LLaDA/main/GUIDELINES.md)).
- **Tooling.**
  - The `dllm` library (Apache-2.0): SFT and LoRA for LLaDA, LLaDA-MoE and
    Dream; diffu-GRPO; AR-to-diffusion `a2d` examples
    ([repo](https://github.com/ZHZisZZ/dllm)).
  - dFactory for LLaDA2.x (FSDP2), and LlamaFactory-based tuning for SDAR.
  - Reinforcement learning: TraceRL / dLLM-RL, d1 / diffu-GRPO
    ([arXiv 2504.12216](https://arxiv.org/pdf/2504.12216)), wd1
    ([arXiv 2507.08838](https://arxiv.org/pdf/2507.08838)).
- **Reported compute.**
  - d1 ran on 4× A5000. wd1's SFT took 2 h on 4× A100.
  - The DiffusionGemma LoRA used rank 4–8, LR 1e-4 to 3e-4, 2k–8k steps, on at
    least 2× A100-80GB. Its PubMedQA LoRA moved accuracy only 75.6 → 76.6, and
    denoising steps rose from 18 to 32, so the fine-tune made inference
    *slower*.
  - No primary source prices SFT on 10k–100k examples. Extrapolated: a few to
    tens of A100-hours, about $100–1,000.
- **Converting an AR model** (if we want our own Ditto base):

  | Method | Training spend | Quality |
  | --- | --- | --- |
  | Fast-dLLM v2 ([arXiv 2509.26328](https://arxiv.org/abs/2509.26328)) | ~1B tokens | Claims to match AR |
  | SDAR | ~50B tokens | Roughly AR parity |
  | Dream | 580B tokens | — |
  | BYOD LoRA ([arXiv 2610.41574](https://arxiv.org/abs/2610.41574)) | ~3–4 GPU-hours | Generation perplexity 8–11 vs 2–3 for the AR parent |

### I.9.3 Serving, speed and determinism

- **Engines.**
  - SGLang has a dLLM framework with LLaDA2.0 support since 2025-12-19
    ([LMSYS](https://www.lmsys.org/blog/2025-12-19-diffusion-llm/)).
  - dInfer v0.2: LLaDA2-flash-CAP at ~581 tok/s at batch 1 on 8×H20
    ([repo](https://github.com/inclusionAI/dInfer)).
  - vLLM supports DiffusionGemma natively and others through a partial plugin
    that rejects grammar-constrained decoding in block mode
    ([plugin](https://github.com/vllm-project/dllm-plugin)).
- **Speed on our shapes.** Our calls are prefill-heavy (4K–32K prompt) with
  short outputs, and the diffusion advantage is in decode.
  - LLaDA2.0-mini commits ~2.55 tokens per forward pass, so a 32–64-token
    reply takes ~13–25 passes instead of 32–64. Prefill costs the same.
  - DiffusionGemma's speed claims use 1,024-token outputs. AR overtakes it at
    ~32 concurrent requests, and one denoising step costs ~3.2x an AR step
    ([report](https://arxiv.org/html/2608.00146v1)).
  - Expect parity to a small win at batch 1, and parity or a loss under load.
- **Determinism.**
  - Temperature 0 with low-confidence (or fixed-threshold) remasking, fixed
    steps and fixed block length is algorithmically deterministic.
  - It is **not** bitwise-reproducible across GPU SKU, batch size, precision
    or cache mode. One study finds dLLM nondeterminism "pervasive and
    structured" ([arXiv 2604.13413](https://arxiv.org/abs/2604.13413)).
  - We found no batch-invariant dLLM serving work.
  - DiffusionGemma's default sampler is stochastic: a random canvas, a
    temperature schedule, re-noising.
- **What validators would pin:**

  | Category | Pin |
  | --- | --- |
  | Model | Weight hash, `mask_id` |
  | Sampling | temperature 0, top-p/top-k off, `cfg_scale` 0 |
  | Remasking | `low_confidence` / `maskgit_plus`, never random |
  | Decoding | block_length 32; fixed steps per block or a fixed threshold (0.95) plus a fixed fallback; editing thresholds for LLaDA2.1/2.2 |
  | Length | `gen_length`, EOS early-stop |
  | Cache | None, or the exact block-prefix cache only |
  | Engine and numerics | bf16, attention backend, engine commit, TP size |
  | Batching | Batch size 1 |
  | Hardware | GPU SKU |
  | Likelihood scoring | Single masked-position logits only; never a Monte Carlo ELBO (LLaDA's default draws 128 unseeded samples) |

  Even pinned, scores must be compared within a tolerance. That is weaker than
  the AR batch-invariant story in Part II §6.4.

### I.9.4 Fit for the decide slot and the chat slot

- **Decide and extract slot.** No structural advantage.
  - A masked diffusion LM can read label logits at one masked position in one
    pass ([LLaDA, NeurIPS 2025](https://proceedings.neurips.cc/paper_files/paper/2025/file/48b383b24230e0e6e649d9c98dae4d8c-Paper-Conference.pdf)),
    but an AR model also reads next-token logits after one prefill.
  - The evidence cuts against diffusion. Putting many labels in one prompt
    shows strong slot bias (the first slot is predicted positive 99–100% of
    the time), so labels must be scored one at a time, which hurts 255-option
    choices ([arXiv 2608.14649](https://arxiv.org/abs/2608.14649)).
  - Calibration and ranking degrade under noise worse than AR
    ([arXiv 2608.08791](https://arxiv.org/abs/2608.08791)).
  - What diffusion *does* offer is bidirectional context for span tagging. An
    encoder (option A) or a bidirectional fine-tune of an AR model gets the
    same thing more cheaply.
- **Chat slot.** Possible, only on the newest bases, with constrained
  decoding.
  - 2025-era dLLMs scored 13.6–28.0 on BFCL-v3 and **0 on multi-turn**, with
    more malformed JSON (["The Bitter Lesson of dLLMs for Agentic Workflows",
    ACL 2026](https://arxiv.org/abs/2601.12979)).
  - LLaDA2.x reaches single-turn parity (vendor numbers).
  - Grammar-constrained diffusion decoding reaches 100% JSON validity
    ([arXiv 2508.10111](https://arxiv.org/pdf/2508.10111)).
  - Automaton-constrained decoding lifts Dream-7B BFCL-Live from 22.3 to 69.0
    sampled ([arXiv 2607.07026](https://arxiv.org/abs/2607.07026)).
  - Neither ships in the vLLM dLLM plugin.

### I.9.5 P0-D: diffusion vs autoregressive, head to head on our decision cases

- **Data.** 50k training and 5k held-out decision examples exported from
  generator gold (I.4 source 1):
  - tool shortlist over v18 menus;
  - route and restraint;
  - note relevance;
  - argument and answer spans;
  - absence.

  Held-out seeds never appear in training.
- **Arms.** The same data, steps budget and LoRA rank (8–16):

  | Arm | Model | Notes |
  | --- | --- | --- |
  | AR-4B | Qwen3.5-4B LoRA | Label logits plus a tag head (the control) |
  | AR-2B | Qwen3.5-2B LoRA | The efficiency control |
  | D-LLaDA | LLaDA2.1-mini LoRA | Masked-position logits plus a tag head |
  | D-SDAR | SDAR-4B LoRA | As above |
  | D/AR-Nemotron | Nemotron-Labs-Diffusion-3B LoRA, scored in diffusion mode **and** AR mode | A same-weights control that isolates decoding mode from data |
  | Reference | Jev 1.14 zero-shot (and Mercury Decide zero-shot, if API spend is approved) | — |

- **Metrics.**
  - Quality: tool shortlist recall@3/@8, route and restraint accuracy, note
    nDCG@8, span exact-match F1 and needed-fact recall, absence Brier, ECE.
  - Speed: p50/p95 latency at batch 1 and batch 16, GPU-seconds per 1k
    decisions.
  - Reproducibility: identical outputs across 3 reruns and 2 batch sizes on
    one GPU SKU.
- **Short-generation sub-test (P0-G, optional).** 2k held-out tool-call cases
  with 3–6 pre-filtered schemas. Measures JSON validity, exact-call accuracy
  and latency to complete output. The diffusion arms run with and without
  grammar constraints.
- **Decision rule.** Diffusion advances to a track only if it matches AR-4B
  quality within noise **and** wins latency or cost at our shapes **and**
  passes the reproducibility check. Otherwise it stays a research lane.
- **Cost.** About $200–800 of rented A100/H100 time (4 LoRAs plus eval), plus
  ≤$20 of Jev. This needs a spend approval; no new infrastructure.

### I.9.6 Recommendation on diffusion

**Yes to diffusion research, no to diffusion first.** The decide and extract
work is one forward pass whichever architecture we pick, so diffusion's
parallel decoding buys nothing there. Its calibration evidence is worse, and
its determinism is weaker exactly where validators need it. The small open
bases are either 16B resident or custom-licensed.

Diffusion's real promise is fast short generation, the "Mercury 3" bet. That
belongs to a later chat-slot track. Miners may enter a diffusion fine-tune
there, if P0-D/G show parity on our tool JSON.

Run P0-D now: it is cheap, and it settles the question with our own data
instead of vendor charts.

## I.10 Product recommendation: the order, and the P0 that proves it

"iPhone mentality" means the model is made for Ditto's job, measured by
Ditto's benchmark, and owned by Ditto. In that frame the order is:

1. **Ditto CPM (decide + extract + pack), autoregressive, 0.8–2B.** It hits
   the special sauce directly, ships fast (8–12 weeks), and serves
   deterministically. It is miner-improvable from day one, because DittoBench
   already holds the labels and the grader.
2. **Open-weight release of the Extract head as "Ditto Extract".** The same
   weights, documented for context pruning. This is the open-weight story
   Peyton asked about, at no extra training cost.
3. **The SN118 Context track.** Part II's mechanism with the CPM as the miner
   slot: shadow, then a share, then lineage.
4. **A small generative model for the chat slot.** AR first. A diffusion
   entrant is allowed if P0-D/G clear it. A single "Ditto Mercury" that packs,
   decides and writes is the long-range goal, not v1.

**P0 (≈3 weeks, research only; each spend needs approval):**

| Experiment | Question | Setup | Win criterion |
| --- | --- | --- | --- |
| **P0-A. CPM vs Jev (the proof)** | Does a Ditto-trained packet model beat general-purpose Jev at building the context packet? | Frozen packet harness (stage 0 recall, stage 2 packer), frozen big model `gpt-oss-20b`, v18 full profile, 5 held-out seeds, plus a LongMemEval-S subset. Arms: (1) no model (today's top-k: 8 notes, semantic top-3 tools); (2) Jev 1.14 doing ranking, pruning and route as in today's hybrid; (3) off-the-shelf LLMLingua-2 / Provence-style pruner; (4) **Ditto CPM v0**: Qwen3.5-0.8B/2B multi-head, S1+S2 on generator gold, ~$50 compute; (5) Ditto CPM v0 + S3 reward (stretch); (6) Mercury Decide `extract` (only with API approval) | **CPM v0 ≥ Jev + 0.02 Q@4K at equal tokens, or equal Q at ≤60% of Jev's tokens.** Needed-fact recall@4K ≥ 0.95. Stage-1 p95 ≤ Jev's per-case decision latency. Bitwise-repeatable outputs on one GPU SKU |
| P0-D. Diffusion vs AR | Is diffusion a better backbone for our decisions? | I.9.5 | I.9.5 decision rule |
| P0-S. Lever size | Does the CPM move the bench enough to run a miner competition on it? | Spread of Q@4K across arms (1)–(5) vs the 0.007 dethrone margin and the paired per-seed sd (0.024–0.045) | Spread ≥ 3–5× the margin; otherwise the track would mostly reward noise |

P0 inference spend is small. About 5 seeds × 6 arms × one 350-case run is ~30
runs, at ≲$1 each under the v18 run budget. The training spend is ≲$100 for
CPM v0 and $200–800 for P0-D. No new infrastructure is needed.

**Product calls only Peyton can make** (also listed in Part II §10). All
four were **resolved on 2026-11-18; see R3.1**. In short:

1. Yes, the CPM is the product's packet builder, hosted by Ditto.
2. Weights are public at submit.
3. Consented team traces form a private track on the real backend.
4. The packet is tuned for production readers, and GLM-5.3-Flash is a v19
   bench candidate.

The original wording follows:

1. Is the CPM the product's packet builder (it replaces the hybrid's Jev calls
   and feeds the router's compression levers), or only a benchmark lane at
   first?
2. Open weights: release Ditto Extract and CPM weights publicly (Apache-2.0),
   only SN118 champions after their reign, or keep them internal?
3. Product traces: may opt-in, scrubbed product traces train the internal
   model and form a private Ditto-only eval?
4. Big-model target: is the packet tuned for the locked `gpt-oss-20b`
   (benchmark), or for the production big model (Claude or another)? A packet
   tuned for one reader may not transfer, so P1 must re-measure on the
   production reader.

# Part II. The SN118 mechanism (revision 1)

## II.1 Where SN118 is today, and what changes

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

## II.2 What "Mercury 3" is

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

**Interpretation (resolved 2026-11-17).** Peyton confirmed the target was
Mercury 3, the generative diffusion LLM, and also likes Mercury Decide's
`extract` mode, which is a token tagger: per-token BIO tags with
probabilities, and "nothing is generated"
([Extract docs](https://docs.inceptionlabs.ai/capabilities/extract)). Both
threads now live in Part I:

- the diffusion question is I.9;
- the Decide + Extract model and the open-weight Ditto Extract are I.8 and
  I.10.

Peyton's sharpened thesis makes the context-packet model the target.

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

## II.3 Prior art: Bittensor and close analogs

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

1. *(Revision 3: R3.2 keeps "private until the commit" and drops "until
   release".)* **Keep weights private until the commit, then until release.** On SN9 and
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

## II.4 Candidate model targets mapped to the bench

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
| **(a) Ultralight LLM** in the chat slot | Qwen3.5-0.8B/2B/4B (Apache 2.0, 262K context; 4B: BFCL-V4 50.3, TAU2 79.9). Gemma 4 E2B/E4B (Apache 2.0). Granite 4.0 1B/3B (Apache 2.0). Phi-4-mini (BFCL 70.3) | 2–10 GB | Decode-heavy, about $0.5–1.5 per run (§II.7) | Replaces `gpt-oss-20b` (20B MoE). Probably a lower ceiling than today's chat agents | Tool-call JSON validity at small sizes. Decode nondeterminism. Biggest per-run cost |
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
   make **deterministic** (§II.6.4). That attacks the dominant source of
   crown noise.
3. The plumbing exists: decision-lane pinning, metering, walls, outage codes
   and the priced-cost line.
4. Ditto gains a model it owns, replacing a third-party lane we pay for and
   cannot inspect.
5. Artifacts are small enough that hundreds of miners share one GPU through
   multi-LoRA serving.

## II.5 Mechanism options

### M1. King of the Hill on a frozen harness (crown)

The miner uploads weights. Ditto serves them, and validators run the frozen
harness against them on ordinary scoring tickets. The crown moves by the
existing sequential paired dethrone.

- Precedents: SN9, SN37, Dippy, Affine era 1, Gradients.
- Strengths: maps one-to-one onto today's machinery. Miners may use any
  training method, including RL against the public generator, distillation
  and synthetic data. The winner is a finished, servable artifact.
- Weaknesses: winner-take-all stagnation (mitigated by the five-way curve and
  lineage releases), copy-and-tweak (§II.6.3), and GPU hosting cost.

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

## II.6 Recommended v1: the Context track (revised from "Decide track")

Revision 2 note: the miner slot is now the **context-packet model** of Part I,
covering decide, extract and pack, not a bare decision model. Everything below
applies unchanged, with three substitutions:

- the wire is `ditto-packet-v1` (I.2);
- the frozen harness is the packet harness (stages 0 and 2, plus the locked big
  model);
- the score is the bench composite at a pinned packet budget B (I.7).

### II.6.1 Contract

- **Frozen harness.** A Ditto-owned reference harness image, digest-pinned,
  versioned as `harness_version` and bound to each bench contract the way a
  generator pack version is. Candidate: the starter kit's v18 hybrid path
  (0.263 on seed 42 at full, $0.27) or a stronger harness built from released
  champion source (§II.10).
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

### II.6.2 Submission

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
- **Store** (*superseded by R3.2: weights are published once the on-chain
  commitment lands and screening passes*). A Platform-owned private bucket (AWS S3 proposal, or the existing
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

### II.6.3 Anti-copy, layered

1. *(Superseded by R3.2: weights are public once committed. Copies are
   handled by earliest-commit priority plus the margin, using layers 2–4
   below.)* **Private until release.** Unreleased weights are never public and the
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

### II.6.4 Evaluation and inference hosting

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

### II.6.5 Emissions

- A new track, `model_context`, beside `memory` and `generator`, folded under
  a new validator protocol. The track-share gate already refuses a share until
  the fleet reports the paying protocol.
- Share path, as the no-inference memo proposed for its track: **0 bps
  (shadow) → about 1,000 bps of the track pool → step up only on board depth
  (≥5 ranked distinct owners) and cost data.**
- Curve: reuse 50/20/13/10/7 and the crown rules. Add a **floor**: a model is
  paid only if it beats Ditto's own reference fine-tune on the same base by
  the dethrone margin. A stock or near-stock model earns nothing, and the
  track burns when nobody clears the floor.

### II.6.6 What a miner does

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

## II.7 Cost estimates (rough, to be replaced by P0 measurements)

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

## II.8 Migration path

### Keep, change, add

| Keep as is | Change | Add |
| --- | --- | --- |
| Bench contracts, generator (public datagen half, private generator slice), block-hash seeds, CRN | The artifact: weights plus a manifest instead of a Docker build context | `harness_version`, frozen and digest-pinned per bench contract |
| Quorum of 3, dueling seasons, sequential paired dethrone, rank shares | Screening: tensor checks and a load test instead of image build and `/health` | Ditto inference pool (multi-LoRA, batch-invariant) behind the relay |
| Canonical outage voiding and re-test (#3566), no-fault relay codes | Copy evidence: tensor hash, ΔW similarity and behavior fingerprint instead of lexical and structural source fingerprints | `ditto-decide-v1` wire with `extract` |
| Copy holds and the released-king alibi pool (#3564), now with a lineage alibi | Daily top-five read: model card, attestation and probe behavior instead of source | Track `model_context`, validator fold protocol, Backroom reads for every new knob |
| Generator competition, now the adversarial data engine | Priced cost by base-size tier | Miner tooling: fixture exporter, local decide server, `practice --decision-model` |
| LongMemEval confirmation | Decision lane must be open on confirmation sessions | Reference fine-tune per base, the pay floor |

The memory (harness) competition **keeps running**. The model track is
additive, like the generator track. Whether the harness track eventually
shrinks is a share decision, not a flag day.

### Phases

| Phase | What | Exit criterion |
| --- | --- | --- |
| **P0. Measure (≈3 weeks, research only)** | Part I §I.10: P0-A (CPM vs Jev at a fixed budget), P0-D (diffusion vs AR), P0-S (lever size vs the dethrone margin) | **Go** if CPM v0 beats Jev on Q@B or tokens, and the spread is ≥ 3–5× the 0.007 margin. Otherwise stop or re-scope |
| **P1. Contract (weight 0)** | Manifest, upload and screening path. Wire and renderer published. Pool on AWS (Terraform plan/apply, protected). Grant pin `miner:<digest>`. Fixtures exporter and local rehearsal | Conformance vectors in CI. A Ditto reference model scores end to end on a canary |
| **P2. Shadow board (≥2 weeks)** | Open uploads, quorum scoring, seasons, copy gate v1, Backroom reads. 0 bps | ≥5 distinct owners ranked. Copy gate false-positive review. Crown stability. Cost per run within estimate |
| **P3. Carve a share** | ~1,000 bps of the track pool. Champion release, base-of-record lineage (M3) | Board depth holds. No unresolved copy rulings. Owner sign-off |
| **P4. Expand** | Chat-slot track (v2), then the one-model "Ditto Mercury" track (v3). Optional open architectures (diffusion) with Ditto-maintained serving adapters. Optional validator GPU spot-checks. Ditto's product decision lane served by the merged champion | Each step is its own contract version and its own shadow period |

## II.9 Risks

- **The lever is too weak.** If the frozen harness caps the score, model
  differences vanish into harness error. P0 is the guard. A stronger frozen
  harness (§II.10) raises the ceiling.
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

## II.10 Open questions for Peyton

Resolved on 2026-11-18 (R3.1):

- **1. CPM in the product?** Yes, hosted privately by Ditto.
- **2. Open weights?** Yes, public at submit (R3.2).
- **3. Product traces?** Yes, from the consented team cohort. Private eval
  on the real backend in Ditto's AWS (R3.3).
- **4. Reader?** Production readers (Claude Haiku 5.5, GPT-6 Luna, some
  larger). `gpt-oss-20b` is being deprecated, and GLM-5.3-Flash is a v19
  bench candidate (R3.4, R3.5).
- **8. Hosting?** AWS, with Lium as an option.
- **9. Release policy?** Public at submit, after the on-chain commit.
- **13. P0 spend?** Approved, about $300–1,000 on AWS credits. Not started
  (R3.7).

New open questions are in R3.8.

Resolved on 2026-11-17:

- **Mercury 3 or Mercury Decide?** Mercury 3. Extract is also of interest.
- **Which slot leads?** The context-packet model (decide + extract + pack),
  per Peyton's thesis.

Product calls (Part I §I.10):

1. Is the CPM the product's packet builder from P1, or only a benchmark lane
   at first?
2. Open weights: release Ditto Extract and CPM weights publicly (Apache-2.0),
   release only SN118 champions after their reign, or keep them internal?
3. May opt-in, scrubbed product traces train the internal model and form a
   private Ditto-only eval?
4. Should the packet be tuned for the locked benchmark reader (`gpt-oss-20b`)
   or for the production big model?

Mechanism and spend:

5. Base allowlist and size tiers: Qwen3.5-0.8B/2B only, or also 4B? LoRA
   only, or also full fine-tunes within the family?
6. Frozen packet harness: stage 0 and stage 2 from the starter kit's hybrid,
   or a Ditto-built reference from released champion source?
7. Budget B and λ pinned per contract (1K / 2K / 4K / 8K)?
8. Hosting: a Ditto pool on AWS (recommended), miner-hosted on Chutes with
   TEE, or validator GPUs?
9. Release policy for champion weights: on crown, after N days, or never?
   This sets how much lineage compounding we get.
10. Emission share and timing for `model_context`. Should it ever draw from
    the memory track's share?
11. Data lane (Affine era 2 style): pay miners for training data that improves
    the base of record?
12. Distillation from closed decision models (Jev, Mercury): recommend
    forbidding it for Ditto's own training and requiring an attestation from
    miners.
13. Spend approval for P0: about $300–1,000 of rented GPU time, plus ≤$50 of
    locked-model and Jev inference. No new infrastructure.

## Sources

Added in revision 3:

- [GLM-5.3-Flash on OpenRouter](https://openrouter.ai/z-ai/glm-5.3-flash)
  (provider prices, API features)
- GLM-5.3-Flash specs (third-party):
  [codersera](https://codersera.com/blog/glm-5-3-flash-complete-guide-2026/),
  [cellcog](https://cellcog.ai/blog/glm-5-3-flash/),
  [DEV](https://dev.to/shaam_ai/glm-53-flash-explained-the-320b-open-weight-model-with-an-18b-brain-and-a-1m-token-memory-2026-5cbl)
- GPT-6 Luna pricing (third-party):
  [TeamoRouter](https://teamorouter.com/blogs/gpt-6-luna-api-pricing-setup),
  [BenchLM](https://benchlm.ai/openai/api-pricing)
- Claude Haiku 5.5 pricing: Anthropic model table (cached 2026-11-03)
- Ditto's private backend and stack repositories (retrieval pipeline, retrieval
  logging, the DittoBench/LongMemEval harness and preview lanes), read for
  this revision and described here without private paths or identifiers


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

External added in revision 2 (spot-checked):

- [Mercury Decide Extract docs](https://docs.inceptionlabs.ai/capabilities/extract)
- [LLMLingua-2, arXiv 2403.12968](https://arxiv.org/abs/2403.12968)
- [Provence, arXiv 2501.16214](https://arxiv.org/abs/2501.16214)
- [LLaDA2.0-mini card](https://huggingface.co/inclusionAI/LLaDA2.0-mini)
- [Nemotron-Labs-Diffusion-3B card](https://huggingface.co/nvidia/Nemotron-Labs-Diffusion-3B)

External added in revision 2 (from the research pass, cited inline in I.9):
the LLaDA, LLaDA2.x, SDAR, TraDo, Dream, DiffuCoder, DiffusionGemma and
Open-dCoder cards and repositories; SGLang, dInfer and the vLLM dLLM plugin;
the dllm library; d1, wd1, Fast-dLLM v2 and BYOD; and the arXiv papers on
dLLM nondeterminism (2604.13413), slot bias (2608.14649), calibration
(2608.08791), agentic workflows (2601.12979) and constrained decoding
(2508.10111, 2607.07026). Vendor benchmark numbers are labelled as such.

Repository added in revision 2:

- Gold labels the CPM trains on: `research/dittobench-datagen/gen/artifact.go`
  (`V10EvidencePairIDs`) and `research/dittobench-datagen/protocol/protocol.go`
  (`DistractorAnswers`, `PrerequisitePairs`)
- [docs/no-inference/extractive_ceiling.py](../no-inference/extractive_ceiling.py)
- [starter-kit PROTOCOL.md](../../miners/dittobench-starter-kit/PROTOCOL.md):
  the `/seed` pairs, subjects and links schema
