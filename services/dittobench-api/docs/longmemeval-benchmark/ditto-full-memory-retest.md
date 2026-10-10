# Ditto full-memory retest protocol

This is an offline research protocol for the private Ditto backend harness, not
a miner submission, production confirmation score, payout input, or a new Mem0
leaderboard comparison. The public repository carries the audit code and
methodology; raw answers, tool transcripts, fixture dumps, credentials,
and provider generation IDs are not copied into this public repository. Raw
reports retain the separately disclosed configured-storage upload behavior;
credentials and fixture dumps are not report payloads.

**Completed September 13:** [independently audited paired results](ditto-full-memory-results-2026-09-13.md)
are OFF 67.2% and ON 72.4%, but graph discovery failed/fell back on 98.93% of
calls. This is an observed flag-condition difference, not demonstrated causal
graph benefit or an unqualified held-out score. Earlier preparation/pending
statements below are retained as the chronological methodology record.

The backend preparation and baseline implementation is tracked in
[backend PR #2679](https://github.com/ditto-assistant/backend/pull/2679).
PR publication does not mean the retest is complete, merged, or deployed.

## Resource pause and authorized resume

The campaign paused on September 12 for a resource decision; the user explicitly
authorized clearing the scoped Go build cache and resuming on September 13.
The [cleanup and resume receipt](results/2026-09-13-ditto-authorized-cache-cleanup-resume.json)
confirms that cleanup completed and native preparation actually resumed; no
full 500-question reader run or new score is implied.
The [immutable pause receipt](results/2026-09-12-ditto-resource-pause-221310.json)
records the exact 169-user native-resume cohort and preserved progress.

At `22:12:45Z` and `22:13:10Z` on September 12, available disk was 2,167,376
and 2,118,236 KiB, both below the 5 GiB stop guard. The task-owned native
process PID 3237 was identified by its exact command and gracefully signaled
with SIGTERM; it drained cancellation records and exited 1. No unrelated
process was stopped, and no cache, fixture, or workspace was deleted.
Headroom subsequently rebounded to 8,322,832 KiB, then an independent reading
near `22:15:11Z` found 6,774,492 KiB. These are point-in-time measurements:
unstable headroom triggered the pause, not a claim the machine still has only
2 GiB free. Remeasure before acting.

The preserved database contains 124,366 pairs, with 83,987 generation and
82,213 storage watermarks set. The interrupted native journal has 105 closed
rows: 97 non-canceled completed attempts (58 strict successes) and 8 canceled
attempts. The remaining native cohort is those 8 canceled scopes plus 161
unattempted users. Completed attempts are not automatically full preparation
successes. Separate settle waves completed 27 and 10 closed users without
receipt exceptions. Canceled in-flight provider calls may have spent tokens
without persisting results; no zero-lost-work claim is made.

No auto-resume or automation was scheduled. At the pause, shared Go build-cache
deletion was not authorized; the subsequent explicit user approval covers the
verified build-cache cleanup below, not unrelated cleanup. After resource
approval and sustained headroom,
use the [committed portable resume guard](../../integrations/longmemeval/resume_backend_campaign.py)
with the preserved backend and frozen backend-graph paths:

```sh
python3 integrations/longmemeval/resume_backend_campaign.py \
  --base /private/task/backend --graph /private/task/backend-graph \
  --adc /private/working-adc.json --dry-run
```

Only after explicit operator approval may `--execute` replace `--dry-run`.
The guard requires the exact frozen source/binary, manifest and configuration
digests, the pinned 169-user cohort, a fresh output path, and three fresh
readings of at least 8 GiB over 60 seconds. It uses explicit checks that remain
active under `python -O`, never clears caches, and defaults to no inference.
The original private wrapper is preserved under SHA-256
`c0c8bf1a746361c26258f3f8d840bdfd88a349974a8a32c65464ae9fa122ae69`;
the portable counterpart removes hard-coded personal paths and strengthens
cohort/guard validation. Its actual preserved-workspace dry run passed.

Remaining phases are native resume from watermarks, remaining settle work,
final graph/label barriers, opaque-ID rewrite plus independent full fixture
audit, frozen prepared-snapshot verification, both 500-question reader arms
using the same `1b575560` executable/source, and independent result auditing.
Retain the latest receipt-bound manifest (SHA-256
`a20eec7a9d772e153403b279faa919e0e24b9ea57fa2f9dc1ab2d6d0636f3095`)
as the resume input. Keep all fixture containers/volumes, immutable audits,
logs, binaries and prior attempts. PRs remain separate from merge/deployment
authorization, and the pause receipt is not benchmark evidence.

### September 13: scoped cleanup and actual resume

The authorized operation was only `go clean -cache` with `GOCACHE` pinned to
the active build-cache directory after directory, canonical-path and `go env`
agreement checks. Immediately before cleanup the cache measured 17,924,968 KiB
(about 17.1 GiB), not a historical larger estimate. Available space was already
308,616,044 KiB at `03:29:37Z`; cleanup exited 0 at `03:29:56Z`, after which
available space was 326,583,984 KiB and the cache directory measured 1,500 KiB.
Only rebuildable build-cache artifacts were removed. Sources, fixtures, module
cache, container volumes and unrelated processes were not touched.

The committed resume guard then passed three fresh headroom readings over 60
seconds: 326,579,920, 326,580,384 and 326,579,456 KiB. Native preparation entered
at `03:32:14Z`, using the unchanged frozen `1b575560` executable/source, exact
169-user cohort, configuration digests and receipt-bound manifest. Process
identity and native pipeline activity were verified, and the first two closed
audit rows both passed. Their immutable prefix hash is recorded separately
from the still-growing full audit. No Go rebuild or fixture reset was needed.

This supersedes only the earlier then-current paused status: the pause and its
resource readings remain preserved. Resume is not completion. The remaining
preparation/blinding/snapshot/reader/audit gates above still apply, and this
operational receipt publishes no new accuracy or spending claim.

## Why retest

The historical isolated-user measurement reported 448/500 (89.6%) with Gemini
3.1 Pro, without running the subject-generation/dreaming stages. Its restored
fixture was also incomplete and had incorrect per-occurrence timestamps (see
the fixture audit below). The score describes that flawed fixture, not faithful
complete LongMemEval-S inputs. The condition did not exercise the full
subject-backed memory system. Gemini is an
experimental reader choice, not a LongMemEval requirement. The new reader
condition is `openai/gpt-5.6-luna` with `medium` reasoning, verified against the
answer-provider response identities, not merely the requested model name.

Two graph concepts must not be conflated:

- Subject summaries and subject-to-memory links support subject search and
  subject-scoped memory reading after dreaming.
- Subject-to-subject edges connect related subjects. Their existence does not
  prove they contributed retrieval candidates or were available to the agent.
  The opt-in experiment adds that retrieval/tool capability separately.

## Conditions and claim boundaries

| Condition | Reader | Preparation | Subject-edge retrieval |
| --- | --- | --- | --- |
| Historical isolated reference | Gemini 3.1 Pro | Incomplete pre-embedded fixture with date errors; no subject dreaming | Off |
| Full-memory Luna baseline | GPT-5.6 Luna, medium | Complete seed plus production dreaming barrier | Off |
| Opt-in graph variant | Same Luna settings | Same completed fixture snapshot | On, bounded opt-in |

Implement the graph variant in a separate checkout while fixture preparation
runs. No full 500-question scoring run had started when the model-visible ID
leakage below was discovered. The final planned comparison uses the same
reviewed compiled source with the graph flag off/on, after opaque-ID repair
and audit. The variant must not change the frozen preparation database or
mutate a running process's source. Freeze each source revision, prompt,
tool catalog, learned-weight binary, dataset, fixture, knobs, and judge before
that condition starts. Record graph bounds and candidate counts in the private
backend evidence. A baseline/variant paired comparison can isolate the intended
graph change only if all other relevant conditions match. A historical Gemini
comparison changes the reader, preparation, source revision, and potentially
other adapter behavior; it is descriptive, not a causal graph-improvement test.

### Restored fixture audit discovered additional input defects

The 2026-09-12 read-only full-history audit supersedes any earlier claim that
the saved fixture was a complete, faithful LongMemEval-S seed. After restoring
71 missing whole-session occurrences, the inspected intermediate fixture had
122,495 pairs representing 244,867 of 246,738 original non-empty turns. It was
still missing 1,871 assistant-first turns (turn index zero) across 1,871 session
occurrences and 484 questions. Five of those occurrences are answer-bearing
sessions; an answer-session flag is diagnostic only, never permission to omit
other histories.

The strict exact-date audit found 23,363 pairs with the wrong per-occurrence
session date, across 4,653 session occurrences and 482 questions, among 23,867 total session
occurrences. Eight affected answer-session occurrences span five questions.
The defect reused a date associated with a shared session ID rather than each
question's specific session occurrence.
This supersedes the earlier 23,350-pair/4,651-occurrence count, which permitted
13 small positive timestamp offsets instead of requiring exact dataset dates.

The new fixture must restore those assistant-first turns and each occurrence's
actual dataset date, then run dreaming afresh. Summaries already generated from
the incomplete/wrong-date fixture cannot serve as the corrected baseline. Keep
the historical and partially dreamed fixtures intact for audit; repair a new
isolated database. Commit the repair/audit procedures and final count/hash
evidence. These additional changes further prevent attributing any historical
score difference to Luna, dreaming, or graph retrieval alone.

### Corrected input fixture verified; dreaming is a separate gate

At `2026-09-12T20:17:18.511581+00:00`, the final strict read-only audit passed
for all 500 isolated questions: 23,867 session occurrences, 19,195 distinct
source session IDs, 124,366 memory pairs, and all 246,738 original non-empty
turns. It verified exact role/text, original sequence in the ordered manifest,
and exact question-specific occurrence dates. Missing/extra turns, wrong dates,
scope errors, missing/unmapped IDs, and duplicate pair references were all zero.

The [sanitized input-fixture audit](results/2026-09-12-ditto-corrected-fixture-audit.json)
records the source report, audit-script, dataset, and manifest hashes without
private database identifiers, paths, case IDs, or content. This verifies only
the input fixture. Fresh dreaming was still the next preparation gate when
this input evidence was recorded; this artifact does **not** establish
completed subject generation, refinement, graph construction, QA, or a score.

### Additional blinding defect: model-visible IDs carried labels

A subsequent inspection found that legacy `firestore_pair_id` values exposed
evaluation annotations: 5,479 pairs had answer-tagged session names, and 7,630
rows belonged to abstention-tagged fixture users. Pair identifiers contained
question IDs, including the `_abs` suffix, and some session IDs contained
`answer_`. Even if raw conversation text is correct, handing those identifiers
to the reader can reveal which memories are labeled as evidence or which
questions are abstention cases.

Consequently, the historical 89.6% result and both one-question Luna smoke
artifacts are not clean, blinded accuracy evidence. Preserve them unchanged as
historical/integration artifacts; do not aggregate or compare their verdicts
as proof of memory quality. The earlier strict input-fidelity audit remains
valid within its exact role/text/order/date scope, but did not test this
model-visible identifier leakage. Its manifest hash describes the pre-blinding
fixture and must not be reused to attest the repaired namespace.

Before any full scoring, deterministically replace model-visible IDs with
opaque identifiers, retain the private mapping for provenance, and independently
audit the new manifest/database. The rewrite must preserve conversation text,
internal row UUIDs, embeddings, timestamps, and subject/graph relationships;
it is not a new semantic seed or an opportunity to tune retrieval. The strict
reader preflight verifies both the selected manifest and actual database IDs
before calling a provider. The implemented scheme is `lme-opaque-sha256-v1`:
each public ID is `lme_mem_` plus SHA-256 of compact JSON containing the scheme,
question ID, session index, and ordinal in the canonical session manifest.
Neither the question ID nor evidence/abstention label is printed in that ID.
Successful strict preflight records
`lme_id_blinding_scheme=lme-opaque-sha256-v1`; the independent result auditor
requires exactly that scheme and rejects missing, legacy, or unknown values.
The rewrite also publishes a new manifest and private preservation receipt;
the fixture auditor supports `require_opaque_ids`, `opaque_ids_valid`, and
`opaque_id_scheme` evidence. Implementation of those checks is not proof that
the rewrite has already been applied or that its final audit has passed.
Dreaming completion and opaque-ID/input
verification are separate gates, both required before the new measurement.

## Dataset, isolation, and the preparation barrier

Use all 500 cleaned LongMemEval-S questions, dataset revision
`98d7416c24c778c2fee6e6f3006e7a073259d48f`, SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
Each question owns a different fixture user/knowledge graph. Shared source
sessions do not authorize cross-question memory. Preserve every timestamped,
non-empty user and assistant turn, including assistant-first, user-only, and
same-role-adjacent histories. The public adapter's `entry_to_pairs` documents
and tests lossless pair conversion. Do not silently discard missing distractor
sessions simply because the answer-bearing sessions are present.

Only history content, roles, IDs, and dates reach seeding/dreaming. Never pass
question text, answers, answer-session IDs, question type, or `has_answer`
labels into memory preparation. Pin answer-time prompt clock to the question's
dataset date. Do not seed current wall-clock dates into historical memories.

Run the backend's production dreaming completion path against a dedicated
loopback-only database. Record a per-user durable preparation audit: memory
count, generation/storage backlog, subject count, subject-memory links, edge
count, graph build state, timestamps, source SHA, extraction model and any
fallbacks. Require complete generation/storage and valid subject/link/build
state before answering. Inspect refinement/orphan recovery errors too: a graph
build marker alone is not evidence every production stage succeeded. An edge
count of zero can be legitimate for a small disconnected graph; report it,
rather than inventing edges or dropping that question.

### Refinement completion requires content-matched native receipts

The legacy pending-refinement query treats a literal ASCII ` | ` in a subject
description as accumulated context awaiting refinement. A successful native
refinement can itself produce that literal delimiter. Counting it again as
unfinished work can therefore prevent a completion barrier from settling.
Removing or replacing that text, exempting selected cases, or accepting the
subject name alone would change the evidence instead of proving completion.

The production refinement observer added in backend `9cc8a942` records a receipt
only after a successful scoped compare-and-save. It is optional and no-op by
default. Its domain-separated semantic digest binds subject ID, user/KG scope,
subject name, exact description (including NULL versus empty), and stored
float32 embedding content. Operational timestamps and key-subject flags are
excluded because later pipeline stages may update them without changing the
refined content. A receipt is an observed native save, not a provider signature
or a guarantee that a later row still matches.

Report three counts distinctly: raw delimiter-matching candidates; receipts
that match those candidates' **current** scoped semantic content; and remaining
pending refinement (`raw - matched`). Only the remaining count may reach the
zero-pending barrier. Changed descriptions, embeddings, names, or scope must
invalidate the old receipt. Generation/storage/key-flag and graph completion
checks remain separate requirements.

The settle command writes a private append-only receipt journal and a new,
exclusive-create output manifest embedding only receipts that still match the
database; it preserves the input manifest. The reader uses that exact output
manifest, whose bytes and receipt-set digest become immutable run provenance.
The current reader metadata contract is
`lme_refinement_receipt_version=native-semantic-save-v1`,
`lme_refinement_receipts_sha256`, `lme_raw_pending_refinement`, and
`lme_accepted_refinement_receipts`. The independent audit requires this version,
a valid digest, nonnegative counts, and `raw == accepted`; it reports the
remaining count separately and requires matching receipt-set hashes across
paired runs. These offline checks validate the recorded contract, not an
independent database read or a claim that every receipt has a provider signature.
Do not claim the receipt path completed a particular case or all preparation
until its native-save journal and current-content audit actually demonstrate
that outcome.

### Preparation interruption and source boundary

Preparation encountered a resource interruption and was resumed with user
concurrency reduced from 16 to 8, retaining the previous attempt evidence and
durable processing watermarks. This changes scheduling, not an authority to
discard incomplete cases or rewrite their content. The final reader source
has advanced beyond the earlier source `66669467e4825904a8fe01668f466328bc21ea08`
to include the receipt-aware completion contract.

The [verified campaign build](results/2026-09-12-ditto-campaign-build-1b575560.json)
freezes source `1b5755609b3ea95660fdba289e6a747adb8c5dae` and executable SHA-256
`695b6c3a0338b902949deb55456a87b17e1ba97e2b92dca63054e187ef75fa21`
(235,116,018 bytes, Go 1.26.5, Darwin arm64). A clean task-local clone with a
`.git` directory produced embedded Git revision matching that source,
`vcs.modified=false`, and `vcs.time=2026-09-12T21:59:33Z`. Both full 500-case
arms are planned to reuse this exact executable/source after all preparation
and opaque-ID barriers pass. The earlier `66669467` preparation executable did
not have embedded VCS stamps and retains its separate, weaker provenance.

Local full-harness and focused graph/receipt/tool checks passed; this is not
proof of CI completion or a reader result. The first receipt-aware settle
launch from the new build failed at startup, before preparation or inference,
because its isolated build clone lacked ignored embedded local configuration.
The recovery path supplies authorized local/common values as process environment
without rebuilding, and must verify precedence and the private loopback database
target before claiming success. At the build-evidence boundary no receipt-aware
case completion or full preparation completion is claimed. Native preparation
using the older executable and resumed concurrency 8 remains a distinct process;
a planned receipt-aware settle at concurrency 2 does not imply it started.

The subsequent [wave-1 settle receipt](results/2026-09-12-ditto-receipt-settle-wave1.json)
does verify runtime execution: the same VCS-stamped binary settled the selected
27 closed users at concurrency 2 between `22:03:53Z` and `22:04:19Z`, with 27
unique successful audit rows and no native warnings. Raw delimiter candidates,
matched receipts, and remaining pending refinement were all zero afterward.
The previously literal-delimiter subject no longer contained the delimiter
after one native refinement; the receipt journal is empty. This verifies the
ordinary zero-pending route, **not** an actual receipt-match exemption. The
receipt-aware contract remains covered by tests, while other users were still
in native preparation. The immutable earlier build artifact's scope is unchanged.

#### Reproduce the frozen binary's configuration boundary

Do not `source` these config files: the native loader is a literal key/value
parser, not a shell evaluator. The successful process loaded local before
common, retained already-present environment values (including explicit database
and ADC overrides), and set no values from quoted shell expansion. Configuration
file digests are retained in the wave receipt; values stay private. Execute from
the frozen clean backend source checkout and use fresh audit/manifest output
paths. The selected cohort is in that receipt's `selected_users` array.

```sh
# Explicit operator values; do not inherit a production/default database.
export SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:54339/ditto_lme_s_final
export GOOGLE_APPLICATION_CREDENTIALS=/private/working-adc.json
export LME_SETTLE_USERS="$(jq -r '.selected_users | join(",")' /path/to/2026-09-12-ditto-receipt-settle-wave1.json)"
python3 - /private/.env.local /private/.env.common /private/dittobench-campaign-vcs-1b575560 \
  -env local longmemeval-dream -stage settle -concurrency 2 \
  -users "$LME_SETTLE_USERS" -manifest /private/input-manifest.json \
  -out /private/new-settle-audit.jsonl -manifest-out /private/new-settle-manifest.json <<'PY'
import hashlib, os, pathlib, subprocess, sys
from urllib.parse import urlparse
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "1b5755609b3ea95660fdba289e6a747adb8c5dae"
assert not subprocess.check_output(["git", "status", "--porcelain"])
binary = pathlib.Path(sys.argv[3]).resolve()
with binary.open("rb") as stream:
    assert hashlib.file_digest(stream, "sha256").hexdigest() == "695b6c3a0338b902949deb55456a87b17e1ba97e2b92dca63054e187ef75fa21"
runtime = dict(os.environ)
config_hashes = ["d2bee32f9749c311831f29637469c6633145ba38379b874e2d891dace2d03377",
                 "6fb161f7c7b714ad851a2242ed0f9987a895321817a1de2c73f244368f54081f"]
for filename, expected in zip(sys.argv[1:3], config_hashes, strict=True):
    data = pathlib.Path(filename).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected
    for line in data.decode().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key:
            runtime.setdefault(key, value)
target = urlparse(runtime["SUPABASE_DB_URL"])
assert target.scheme in ("postgres", "postgresql") and target.hostname == "127.0.0.1"
assert target.port and target.path.startswith("/ditto_lme") and not target.query and not target.fragment
assert runtime.get("GCLOUD_PROJECT") and runtime.get("GOOGLE_APPLICATION_CREDENTIALS")
os.execve(str(binary), [str(binary), *sys.argv[4:]], runtime)
PY
```

This is the parser and executable boundary used by the successful phase, not
permission to repeat settled users to improve an answer. Hashes and a clean
checkout protect source identity; the native stage still checks eligibility
and private-database scope. Preserve both the startup failure and successful
attempt, and do not replace the new manifest with the earlier input manifest
when proceeding to later preparation gates.

## Known learned-retriever overlap

The backend repository's retrieval training dataset contains 474 unique
LongMemEval question IDs; all 474 overlap the exact cleaned 500-question set.
The checked training script's `val_frac=0.1`, `seed=1234` split yields 427
training and 47 validation rows. The inspected embedded `model.bin` SHA-256 is
`46d34091333706c841b6e0f6b35b2bf9f5f89ac0fca692720ce9c22cc795138c`.
Its metadata references `model.pt`; dataset overlap alone does not establish a
complete, independently verified training-to-export lineage for that binary.

This is substantial training/evaluation overlap and precludes an unqualified
held-out, unseen-test, or apples-to-apples Mem0 superiority claim. Preserve and
report the production weights for the requested best-system measurement. A
future uncontaminated assessment needs independently held-out histories or a
separately labeled retriever trained without these evaluation questions. Do
not tune on failures, change weights, and then label a rerun the same condition.

## Answering, judging, and interruption policy

### September 13: preparation closed and matched readers started

The [immutable preparation and first-attempt handoff](results/2026-09-13-ditto-prepared-opaque-reader-handoff.json)
records completed native preparation plus bounded settle, forced graph rebuild
500/500, and synchronous label processing 500/500 (closed
`2026-09-13T05:05:35.657463Z`, zero captured native warnings). Ten settle waves
closed 113 unique users. The resumed native phase itself retained 96 complete
and 73 incomplete barrier attempts and exit 1; those 73 users subsequently
settled. This is not a claim that the original native pipeline was flawless.
All pending generation/storage/refinement/key-flag counts are zero. The actual
refinement proof set remains empty: raw pending and accepted receipts are both
zero, so no receipt exception was used. Of 10,731 clusters, 8,822 have LLM
labels; 1,909 empty labels remain explicitly `intentional-or-unresolved`.

After all writers drained, opaque-ID rewriting preserved non-public-ID memory
columns and derived subject/link/edge/cluster state. The independent full
fixture audit passed at `2026-09-13T05:15:55.623468Z`: all 500 cases, 124,366
pairs, 246,738 original turns and 23,867 session occurrences, with zero missing,
extra, scope, sequence or timestamp errors and every public memory ID opaque.
Final manifest SHA-256 is
`a232c2b7ee77f06597535416682c00187f9c44bb2a324de7ccd5dce3fa13ccef`.
The independent read-only snapshot completed at `05:21:05Z`, with digest
`01c6a7070cba4e1455894c6da07132dcbba1b5cbc37c2c58c9f26db4adb3d055`.
The linked evidence pins all ten table counts/hashes and source artifacts.

Both readers use source `1b5755609b3ea95660fdba289e6a747adb8c5dae`, the
previously attested binary SHA `695b6c3a0338b902949deb55456a87b17e1ba97e2b92dca63054e187ef75fa21`,
and the same pinned runtime configuration. With `LME_BASE` and `LME_GRAPH` set
to the absolute preserved backend/backend-graph clone paths, and the exact
local-then-common set-if-absent configuration boundary above already loaded,
the reader commands are:

```sh
"$LME_GRAPH/.tmp/lme/dittobench-campaign-vcs-1b575560" -env local longmemeval \
  -data "$LME_BASE/.tmp/lme/data" -models openai/gpt-5.6-luna \
  -reasoning-effort medium -judge-model google/gemini-3.1-flash-lite \
  -prompt-clock question-date -manifest "$LME_BASE/.tmp/lme/seed_manifest_opaque.json" \
  -require-graph -concurrency 8 -out "$LME_BASE/.tmp/lme/runs-final-off" \
  -checkpoint "$LME_BASE/.tmp/lme/evidence/qa-final-off-1b575560.jsonl"

# Run ON only after OFF completes and the independent snapshot comparison passes.
"$LME_GRAPH/.tmp/lme/dittobench-campaign-vcs-1b575560" -env local longmemeval \
  -data "$LME_BASE/.tmp/lme/data" -models openai/gpt-5.6-luna \
  -reasoning-effort medium -judge-model google/gemini-3.1-flash-lite \
  -prompt-clock question-date -manifest "$LME_BASE/.tmp/lme/seed_manifest_opaque.json" \
  -require-graph -subject-graph -concurrency 8 -out "$LME_BASE/.tmp/lme/runs-final-on" \
  -checkpoint "$LME_BASE/.tmp/lme/evidence/qa-final-on-1b575560.jsonl"
```

OFF attempt 1 closed normally without an operator interruption at
`2026-09-13T05:46:42.086389Z`, exiting 1 because only 497 of 500 attempted cases
were judged. The immutable report identifies exactly `gpt4_15e38248`,
`46a3abf7`, and `88432d0a` as unjudged, each with
`agent loop: remote error: tls: bad record MAC`. Its before/after snapshot hashes
both equal the independent reference. The strict incomplete-run guard prevented
report upload. Resume the identical command/checkpoint for only those three
unjudged failures; never rerun the 497 judged answers or treat transport failures
as incorrect answers. Preserve this incomplete attempt alongside the resumed
report. The slow last case completed without intervention; earlier concern
about a stalled stream did not require a timeout or signal. No completed
500-case accuracy, graph gain, or monetary spend is claimed by this handoff.

The first resume invocation then failed during bootstrap because its database
hostname was mistyped as `postgres`, producing `lookup postgres: no such host`.
It exited before reader preflight/inference and did not change the original
497-row checkpoint. The corrected resume restored the required `127.0.0.1`
loopback target and used a separate log. Both attempt logs are preserved; this
operator error is not an answer failure or evidence of a changed scored
condition. The immutable handoff records the failed log's hash.

Record requested and observed answer model/provider for every successful
provider turn, reasoning effort, prompt clock/time, graph preparation flag,
graph retrieval flag, tool names, fixture user, hypothesis, and explicit judge
status. A provider/query/judge failure is not a wrong answer and must not be
silently counted as a valid negative judgment. Preserve empty final answers in
raw attempt evidence; this strict condition treats them as incomplete and
does not certify a headline aggregate containing them. Do not choose a more
favorable answer from private reasoning.

Report effective graph use separately from the enabled flag. The independent
auditor exports `lme_subject_graph_failures / lme_subject_graph_calls` as the
discovery failure/fallback rate, candidate **occurrences** (not globally unique
memories), cases with graph-discovered seed IDs, and explicit
`explore_subject_neighbors` trace calls/cases/truncations separately. Frozen
`pkg/services/retrieval/subject_graph.go` records those discovery counters around
the best-effort candidate stage with a two-second maximum SQL statement budget
inside its 2.5-second discovery context, plus cleanup; failures fall
back to stock candidates. Explicit neighbor-tool calls are outside these
counters. Graph seed IDs may also have been discovered by stock retrieval, so
they are not evidence of graph-only additions. A graph-enabled result is not
automatically an evaluation with effective graph coverage on every case.

Counters describe the final invocation, not necessarily all resumed attempts.
Resume re-prepares seed contexts for all 500 cases before skipping already
judged reader pairs, so retrying three answers does not mean only three total
embedding/provider calls. Recompute latency/token summaries from all 500
`per_case.data` observations; the resumed report's Standard/Speed block contains
only new-invocation samples. The auditor reports full-case coverage and uses
nearest-rank p95. These are selected successful observations, not failed-attempt
cost, separate judging/preparation work, or total campaign wall time. Preserve
both invocation intervals and the original slow tail separately.

Checkpoint/resume must reject duplicate IDs and condition changes. Retry
operational failures only, retaining attempt evidence; do not resample judged
incorrect or empty native answers to improve a score.

Pin and name the built-in judge separately from the reader. Its boolean QA
accuracy is the headline for that condition, not the backend's 60/40
QA/session-recall composite. Session recall is a separate diagnostic. The
official post-hoc comparison is an additional judgment of the identical frozen
hypotheses using LongMemEval source revision
`9e0b455f4ef0e2ab8f2e582289761153549043fc`, unmodified evaluator/prompt and
`gpt-4o-2024-08-06` through the existing narrow judge proxy. Do not relabel a
built-in judge score as official. No evaluator inference is launched by the
audit script.

### Existing full-report storage publication

The frozen `1b5755609b3ea95660fdba289e6a747adb8c5dae` reader retains the
standard harness upload after saving a successful local report. Despite the
legacy `UploadToB2` function/log name, FileStorage selects the configured
provider: the earlier graph smoke logged a successful Hippius destination at
`https://s3.hippius.com/ditto/dittobench/dittobench/<run-filename>`. This is the
**complete report, not a sanitized aggregate**. It includes public LongMemEval
questions and gold answers, generated hypotheses and judge rationale, memory
and subject tool arguments/results, fixture and retrieved-memory IDs, provider
response IDs/model/provider, source and condition hashes, and CLI data-directory
and manifest paths (which may be absolute local paths). Tool text is capped at
16 KiB per argument record and 64 KiB per result, with full-text hashes and
explicit truncation flags; this is a size bound, not a privacy filter.

Source review found no serialization of API/storage credentials, ADC contents,
environment values, or the reader's hidden reasoning stream. Explicit
tool-argument reasoning and judge explanations can appear. Executable tools
are restricted to fixture-scoped memory operations; the prepared snapshot
contains benchmark user IDs and table counts/digests, not raw database rows or
embeddings. A credential-pattern scan of the earlier one-case smoke found no
matches, but that artifact had no tool traces: the expanded trace assessment
is source-based, not a completed 500-case output audit. No material private-data
leak was identified for this isolated public-data fixture; that conclusion
depends on the fixture/isolation gates and is not a general report sanitizer.

Strict incomplete runs return before upload; complete runs upload before the
independent offline audit. Upload failure is a nonfatal warning and the local
report remains. The frozen CLI has no upload opt-out. The returned object URL
is unsigned, but bucket ACLs and unauthenticated readability were not tested;
do not infer public or private access from the URL alone. This review made no
new upload, inference call, or backend change. Full matched-reader scoring
remains gated on preparation completion, opaque IDs, and the frozen snapshot.

## Independent prepared-state capture

After **all** preparation, graph, labeling and opaque-ID rewrite processes exit
and the final opaque fixture audit passes, independently capture the frozen
state with the read-only utility below. Do not capture an in-progress state as
the final reference. Replace the manifest/output paths with the preserved task
artifacts; the output must not already exist.

```sh
python3 services/dittobench-api/integrations/longmemeval/capture_backend_snapshot.py \
  --manifest /path/to/backend/.tmp/lme/seed_manifest_opaque.json \
  --container ditto-postgres-lme-luna-20260912 \
  --database ditto_lme_s_final \
  --output /path/to/evidence/independent-prepared-snapshot.json
```

The utility independently reproduces frozen backend source
`1b5755609b3ea95660fdba289e6a747adb8c5dae`'s
`pkg/dittobench/longmemeval_snapshot.go`: one PostgreSQL repeatable-read,
read-only transaction with UTC timezone; the same ordered ten-table allowlist;
SHA-256 of every scoped `to_jsonb(row)::text`, then SHA-256 of the sorted
concatenation of those row hashes. It uses sorted, unique, exact
`lme_s_<question_id>` owners from all 500 manifest cases. The final digest hashes
compact JSON in the Go struct's exact field order, including its initially
empty `sha256` field. Output contains only scoped users, table names/counts,
and digests, and a new output file is created with mode `0600`.

Repeat after each reader into a **new** output file with
`--compare /path/to/evidence/independent-prepared-snapshot.json` to fail on any
state difference. `--compare` also accepts a completed backend report and
compares its `meta.lme_prepared_snapshot` object to the independent capture.
The backend separately compares its own before/after snapshots. Neither
mechanism prevents concurrent writers; the drained-writer barrier remains
mandatory. Pure unit tests verify transaction construction, fixture-scope and
table validation, Go-compatible serialization, comparison, and non-overwriting
private output; no Go rebuild, inference, or database mutation is required.

## Reproduce the offline audit

### Fresh-checkout input prerequisites

The audit below operates on completed evidence; it does not seed a database.
Likewise, backend `longmemeval-hydrate` repairs an existing isolated fixture,
not an empty database: it requires a 500-case source manifest and existing
users/pairs. A local database name or a `SOURCE/seed_manifest.json` placeholder
is not a reproducible source location. The existing bootstrap inputs are:

| Input | Immutable source |
| --- | --- |
| Public cleaned dataset | [Hugging Face file at pinned revision](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json), digest above |
| Private pre-embedded histories | `ditto-assistant/ditto-backend-ops-log` at `667552e2386b9e647f1f5e3d12b77ffe9e9a99ca`, directory `longmemeval-full/` |
| Shard manifest | `longmemeval-full/manifest.json`, SHA-256 `c5323906cab920dbc4d32d32db6c34c2f4b7910794847c8f4cf928bda5933edf`; 11 shards, 99,053 source pairs, Vertex `text-embedding-005` |
| Existing isolated-fixture builder | `ditto-assistant/heyditto-stack` at `b5b8cf3cd9da00efbcbb6a0c0027428d44e8ee6d`, `.agents/skills/longmemeval-bench/scripts/build_lme_s_fixture.py`, SHA-256 `1371edb4acd00593b7decce27de22ada1335e171e2bb471e136d097c822c3109` |

The two private repositories require authorized access. The builder verifies
every compressed shard against its manifest before producing `users.tsv`,
`memory_pairs.tsv`, `seed_manifest.json`, and `stats.json`. No copy of the old
operator's Docker volume is required if these pinned repositories are available.
The builder intentionally reconstructs the historical incomplete 122,416-pair
starting point; its output is **not** the corrected 124,366-pair final fixture.

The complete bootstrap sequence is: check out the pinned inputs; download and
verify the dataset; create a fresh task-specific, loopback-only pgvector
database; initialize `vector`, `uuid-ossp`, and `pgcrypto` extensions; run the
selected backend revision's Postgres migrations; run the pinned builder; import
its users and memory pairs; then execute hydration, the exact-date/turn audit,
audit-authorized repair, and the final strict audit before dreaming. The builder
emits all 500 isolated users. Import its users first, then use these columns for
the TSV COPY (Postgres text format):

```sql
-- Execute only against the newly created isolated benchmark database.
\copy users (uid, balance, first_name, plan_tier) FROM 'BOOTSTRAP/users.tsv' WITH (FORMAT text)
\copy memory_pairs (firestore_pair_id, user_id, kg_id, prompt, response, conversation_embedding, session_id, source, timestamp) FROM 'BOOTSTRAP/memory_pairs.tsv' WITH (FORMAT text)
```

`go run ./cmd/dbmgr -env local migrate` is the current backend migration
entrypoint; explicitly supply the isolated loopback `SUPABASE_DB_URL` and do
not use a shared local reset. Backend env files and working Application Default
Credentials are required for Secret Manager and newly recovered Vertex
embeddings. The reader's local OpenRouter key alone does not satisfy those
embedding/bootstrap dependencies. Use a data root containing
`longmemeval/longmemeval_s_cleaned.json` for the final QA command, rather than
pointing it at the file itself or the historical oracle dataset.

This is a source-reviewed reconstruction recipe, not a claim that a second
from-zero bootstrap was executed during the current retest. The measured task
restored the preserved historical database and then repaired it. Retain the
full source revision, migration state, exact input hashes, and final fixture
audit to distinguish those routes. The builder stamps a new `created_at`, and
new embeddings/dreaming invoke hosted models: a rerun can reproduce the method
without producing byte-identical manifest hashes or generated summaries.

### Cost validity

The current backend price table lacks a Luna entry and can substitute a generic
price. The run metadata therefore records `lme_cost_estimate_valid=false` and
`lme_cost_estimate_invalid_reason=unknown_answer_model_price`. Do not quote the
resulting generic-fallback monetary estimate as Luna cost, billed spend, or an
invoice. The auditor exports no monetary amounts and preserves this validity
warning. Provider-reported usage/billing and dreaming cost require separate
evidence; neither is established by reader token counters.

### Audit completed reader evidence

From `services/dittobench-api/integrations/longmemeval`:

```bash
python3 audit_backend_run.py \
  --dataset /private/longmemeval_s_cleaned.json \
  --run /private/luna-full-memory.json \
  --condition ditto-lme-s-full-memory-luna-medium-v1 \
  --judge-model '<exact-built-in-judge-model>' \
  --output /private/luna-full-memory.audit.json \
  --export-hypotheses /private/luna-full-memory.hypotheses.jsonl
```

The CLI requires a final JSON report with a full source commit and actual
SHA-256 prompt/tool/learned-weight digests. It also requires standalone manifest
and selected-case digests, a prepared-fixture snapshot captured before/after
answering, identical before/after fingerprints, and an explicit unchanged
attestation with no snapshot error, plus the exact implemented ID-blinding
scheme and receipt-aware zero-remaining-refinement evidence. Source-evidence
metadata is retained without imposing an unagreed wire
format. Its loading utility understands
JSONL checkpoints for diagnostics, but a checkpoint alone cannot attest source
provenance and is rejected for a publishable audit. The auditor
rejects incomplete/duplicate/unexpected IDs, unknown/failed judgments, changed
reader/provider/reasoning/clock/graph flags, repeated provider IDs, shared
fixture users (including swapped question fixtures), reasoning-derived or
empty final answers, and inconsistent reported QA accuracy. All 500 IDs must pass;
there is no `--allow-partial` or legacy-evidence relaxation. Historical reports
without this provenance remain historical, not retrospectively certified.

For graph-on evidence add `--graph-retrieval` and use a distinct condition.
To compare opposite graph conditions under otherwise identical checked reader,
judge, reasoning, and date settings, add `--paired-run /private/other.json`.
The requested run is the left side and paired run the right side. The auditor
also requires equal learned-weight and system-prompt hashes. Source and tool
hashes may legitimately differ for the graph implementation. Seed-manifest,
selected-case, receipt-set and prepared-fixture snapshot digests must match; a mismatch or
one-sided digest fails closed. An additional raw-dataset digest is compared
when recorded; if absent it is explicitly unavailable, not treated as matching
(the local pinned dataset and exact recorded questions are checked separately).
The combined condition digest
is not compared across runs because it includes their different source commits.
Graph parameters still need manual review. Snapshot hash equality attests the
recorded fingerprint scope, not a broader deployment identity; matching question
IDs, accuracy, paths, or flags alone does not prove identical memories.

Only the aggregate audit is safe to publish after review: it includes hashes,
per-type QA counts, empty-answer count, Wilson intervals, aggregate tool usage,
provider turn counts, and optional paired disagreements/exact McNemar test.
Wilson intervals describe finite-case Bernoulli uncertainty, not repeated-run
model variance; overlapping histories further limit independence. A single run
cannot establish stochastic robustness or guarantee a graph gain.

```bash
python3 -m unittest -v
```
