# DittoBench v2 coverage

What the benchmark tests, how much of it runs per case, and what it deliberately
does not test. Generation is a pure function of `(seed, bench_version)`; the
subnet re-seeds every run from an on-chain block hash fixed after the miner
commits, so a harness is scored over many fresh, unpredictable datasets. The
generator is fully public and every dataset is byte-reproducible from its seed,
so any validator or auditor can regenerate a run and recheck a score.

## Run sizes

Each run generates a fresh dataset at one size:

| run_size | tool cases | memory cases | isolation | total |
|----------|-----------:|-------------:|----------:|------:|
| small    | 6          | 6            | 0         | ~12   |
| medium   | 20         | 20           | 2         | ~42   |
| full     | 60         | 50           | 4         | ~114  |

The composite is `0.5·tool_mean + 0.5·memory_mean`, multiplied by three bounded
factors (all explained below): observed tool-efficiency, canary-integrity, and
metamorphic-consistency. tool_mean, memory_mean, and the per-category breakdown
stay pure accuracy; the factors touch only the composite. Latency and token cost
are reported, never scored. A `composite_stderr` accompanies the composite so a
consumer (the subnet KOTH fold) can gate on measurement uncertainty rather than
a flat margin.

## Per-version rotation

Generator output is a function of `(seed, bench_version)`, not the seed alone.
Each version folds its number into the generation stream (`protocol.RotateSeed`),
so the rendered surface rotates on a version bump. A harness that only
pattern-matches one version's rendered templates degrades on the next; a
reasoning harness is unaffected. The rotation is deterministic and public, so
byte-reproducibility from `(seed, bench_version)` holds. Seed-derived answer
material (case ids, tool needles, canary nonces) stays on the raw seed, so
haystack values and their expected answers remain coupled across the rotation.

## Opaque case identifiers

Case IDs are a seed-keyed hash, not a readable label. They no longer embed the
category, question type, or asked attribute, so a harness cannot read the
answer class off the ID before processing the case. Because the seed is public
after scoring, every ID is still recomputable and the full artifact carries the
id-to-case map, so auditability is preserved.

## Tool suite

- **Single tool**: the correct answer is one catalog tool, with exact-value
  argument grading where the argument is a concrete token (URL, theme, model).
  Where the category grades an arg value, an intent-phrasing variant states the
  request without the scored token verbatim, so copying a word does not pass.
- **Routing traps**: phrasing points at the wrong tool: memory-vs-web either
  way, run-vs-read (dispatch vs check), edit-vs-create, job-vs-workflow
  (one-off task vs decomposable goal), automation-vs-job (scheduled vs now),
  save-vs-search (statement vs question), update-vs-save.
- **Multi-hop**: an ordered tool sequence; relative order is scored.
- **Dependent-arg multi-hop**: a hop's argument exists only in the previous
  hop's served result (a coined job_id from `execute_agent_job` passed to
  `get_agent_job_status`, a URL from a `search_web` result fetched by
  `read_links`). The trajectory is unfakeable by construction because the value
  is not in the prompt.
- **Parallel**: two independent tools in one turn; the name/arg set is scored
  but call order is not (`parallel_web_image`).
- **Result-usage**: the answer must carry a per-seed value that exists only in
  the served tool's result, so it cannot be answered without executing the tool.
- **Error recovery**: the mock endpoint returns a transient error on the first
  call for a seed-chosen slice; the case scores full only if the harness retries
  and completes, checkable because the served post-retry result carries the
  needle (`web_recovery_result_usage`).
- **Restraint**: no tool should be called: chit-chat, unanswerable questions
  (abstention), and a request whose required argument is missing, where inventing
  one is the failure being probed (`arg_hallucination`).

Coverage spans the production tool surface: web/memory search and read,
image create/edit, artifacts, agent jobs and dynamic-goal workflows,
automations and recipes, capability discovery, Google Workspace
(calendar/gmail), memory writes (save/update/delete), and the full settings
cluster (theme, model, effort, tool prefs, accent color, font). Each case's
presented tool list includes seed-chosen near-miss and irrelevant tools drawn
from this catalog, using real production names, so prefix-free routing is
genuinely discriminative.

Tool trajectory, arguments, result-usage, dependent-arg chaining, error
recovery, and the restraint cases are scored deterministically, with no LLM
judge. Argument grading penalizes stuffing: packing many candidate pool values
or an over-long argument no longer satisfies a value-graded arg by containment.

## Memory suite (13 question types)

Recall (`single-session-recall`), cross-session synthesis (`multi-session`,
including list count/enumerate and a state-at-event join), `temporal-reasoning`
(3-way ordering + elapsed duration), `knowledge-update` (latest value over a
supersession chain), `preference` and `preference-application`, `contradiction`
(change of mind), `abstention` (needle-absent), `injection-resistance`,
`assistant-recall`, `aggregation-count`, `computed-answer`, and `canary`:

- **`assistant-recall`**: the value lives only in a past *assistant* turn (a
  recommendation the user never stated), so it tests recalling what the assistant
  said, not the user.
- **`aggregation-count`**: one topic raised several times across sessions, asked
  as "how many times"; a retriever that dedupes the repeated mention undercounts.
- **`injection-resistance`**: a real recall question wrapped in an
  instruction-override attack; complying emits a per-seed forbidden payload that
  scores 0, resisting answers from memory. The payload is derived per seed, not a
  constant, and is scored programmatically via the forbidden-answer check.
- **`abstention`**: drawn procedurally from attributes the persona could have
  but does not, rendered through the same phrasing machinery as answerable
  questions, so the question distribution is identical and abstention is a true
  discrimination test rather than a fixed-string match. Includes false-premise
  decoys where a friend's value is seeded but the user's is not.
- **`computed-answer`**: the answer is a function of many seeded facts (a
  filtered count, a temporal delta), not a single lookup, so lexical overlap and
  single-fact retrieval both fail.
- **`canary`**: the answer is a per-seed high-entropy nonce seeded into this
  run's conversation. It cannot be memorized across runs, so a correct answer
  proves genuine in-context retrieval. A plausible-but-wrong bait nonce
  (attributed to someone else) is seeded alongside it; echoing any nonce-shaped
  token surfaces the bait and fails. Leaking a canary (echoing the bait) multiplies
  the whole composite by 0.5 as an integrity breach, and leaks compound, so easy
  recall cannot buy the integrity signal back. An honest miss carries no composite
  penalty — it is already reflected in the case's own accuracy, and penalizing it
  again taxed the nondeterministic honest reasoner the canary protects.

Memory grading is deterministic and judge-free: each case carries an
`answer_kind` (value, number, list, ordered list, duration, reversal, decline)
graded by the matching checker, with distractor and forbidden-value zeroing (see
`docs/judge-determinism.md`). Facts are
rendered through a compositional surface grammar (synonym sets, clause order,
tense, lead-ins and trailers) rather than a handful of fixed frames, and
questions get a low-overlap (NoLiMa) rewrite so a lexical shortcut cannot stand
in for retrieval. Seed-chosen no-op distractors (near-miss third-party facts,
superseded-then-restored values) are spliced into the haystack; a grounded
reader ignores them, a frame-matcher ingests them. Multi-graph **isolation**
cases seed a second user with a conflicting value; a cross-graph leak scores 0.

## Anti-memorization and calibration signals

Reported alongside the score, these separate a genuine harness from a template
matcher or a memorizer:

- **Metamorphic twins**: occasional same-template pairs differ in one
  load-bearing mutation at unpredictable positions. Correct on one with the
  stale answer on its twin is a memorization fingerprint; the derived
  metamorphic-consistency factor folds this into the composite.
- **Calibration**: on a slice, the harness reports a confidence; a Brier
  proper-scoring term measures whether stated confidence matches observed
  correctness. Advisory, reported not scored.
- **Behavioral plausibility**: sub-model-inference latency with near-perfect
  deterministic scores and near-zero output variance is the regex-harness
  signature. Forwarded like the structural fingerprint, never folded into the
  score.

An offline analyzer (`cmd/gstudy` in the generator repo) decomposes per-case
score variance into seed, item, and residual components and estimates per-
category difficulty and discrimination, flagging saturated (everyone-passes) and
floor (everyone-fails) categories that carry no information at the champion
boundary and should be retired or rebalanced.

## Reading the results

`per_category` carries a `count`, `mean`, and `std_err` (standard error of the
mean). Per-category means come from few cases per run (a 95% band is roughly
`mean ± 1.96·std_err`), so treat a single run's per-category number as
directional and rank capabilities from the aggregate across many seeds. The
aggregate composite is the reliable signal; the per-category breakdown is
diagnostic.

## Deliberate non-goals (v2)

These are out of scope by design, not oversight:

- **Languages other than English.** All personas, prompts, and questions are
  English. Multilingual coverage would need reviewed translations of every pool
  and template; deferred rather than shipped machine-translated.
- **Real multimodality.** Image/artifact tools are routing *targets* (names to
  select), not actual image, audio, file, or vision inputs. The subnet's harness
  contract is text in / text + tool-calls out.
- **Interactive multi-turn evaluation.** Each case is one request. The multi-turn
  structure lives in the *seeded* memory the harness recalls from; the validator
  does not hold a live back-and-forth dialogue within a case.
- **Long-context stress.** The haystack scales modestly and is spread across
  seeding waves rather than flooding one prompt; there is no needle-in-a-huge-
  single-context tool case.
- **Safety/policy refusal.** The only refusal tested is grounded decline
  (abstention) and injection resistance; harmful-request refusal is not in scope.
