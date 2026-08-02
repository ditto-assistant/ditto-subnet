# v7 proactive overfit screening

Proactive source-review coverage for the v7 overfit exploit classes surfaced by
the v7 rebench (the v6 rank-#1 harness "whitycatboss" collapsing 0.957 -> 0.59,
plus the byte-identical cross-hotkey scratch-harness trio). This work adds
location-only semantic leads, an advisory binary saturation statistic, and
agentic-reviewer prompt guidance. It changes nothing on the deterministic-reject
path and does not bump `SCREENING_POLICY_VERSION`: every screen here routes to
operator **quarantine**, a **behavioral audit**, or an **advisory lead** only.

Read this together with [source-review-policy.md](source-review-policy.md) (the
allowed-optimization boundary, the benchmark-emulation / scorer-contract
evidence threshold, the two-distinct-location rule, and the DittoBench v3
preflight exception) and
[policy-v7-false-positive-regression.md](policy-v7-false-positive-regression.md)
(the canary-rollout precedent).

## Non-negotiable constraints honored here

- **False positives are the hard failure mode.** Legitimate generic
  optimization — lexical/user-scoped retrieval, faithful answer-slot
  serialization, prompt-injection defenses, tuned prompts + a genuine model
  call, official fixtures with valid provenance — must not be flagged. Every new
  lead ships with a suppressor and/or a prompt discriminator whose synthetic
  legitimate look-alike is asserted lead-clear in the tests.
- **No auto-reject on behavioral/source signals.** Only the objective stable
  core (`find_decisive_malicious_source`) can pre-build `deterministic_reject`
  (unsafe archive / Rust / build / health). Nothing here touches it. Overfit /
  emulation signals may only route to quarantine, a behavioral audit, or an
  advisory lead.
- **Locations/roles only.** `source_signals` leads return `path:line` + role
  names, never source text or matched values. `binary_analysis` reports bucketed
  counts, never the matched tokens. Tests assert the privacy boundary.
- **Two distinct validated locations** remain required for `benchmark_emulation`
  and `scorer_contract_manipulation` (`_MULTI_LOCATION_CATEGORIES`); a lead is a
  search prompt, not a finding.

## The co-occurrence engine and its new suppressors

`ditto_screener/source_signals.py` `find_source_review_leads` matches a rule
when every role hits within an 18-line window of a common anchor. This change
adds an optional `suppressors` tuple to `_Rule`: if any suppressor role also
matches within the window, the lead is dropped. Suppressors encode the
verifier's FP guards structurally (a genuine model call in-window, an
injection-defense strip sink, the request user's own message, a documented
list/history family, a single-value resolution) so a lead does not fire on the
honest look-alike. This keeps leads as routing signals, not verdicts.

---

## Exploit catalog and mapped screens

Each entry: the exploit mechanism, its source signature, the legitimate
look-alike that must be spared, and the mapped screen with its outcome and
implementation status. "Lens" mirrors the analysis taxonomy.

### Implemented now (location-only leads + prompt clauses, route to quarantine)

#### 1. Pre-model deterministic resolver feeding the faithful answer-slot
- **Mechanism.** A resolver recognizes a scored template, computes the expected
  value from a table / generator-mirror, injects it into model context as
  `ANSWER: <value>` (or overrides the slot post-generation), and the faithful
  extractor lifts it verbatim. The serialization is "faithful" to a value that
  never came from genuine inference.
- **Source signature.** A challenge-template resolver co-located with an
  answer-slot writer/override and a trailing-answer extractor.
- **Legit look-alike.** Lifting the model's OWN trailing `ANSWER:` line;
  supplying computed temporal/count/ordering context to a real model that can
  override it (both explicitly allowed).
- **Screen.** New lead `precomputed-answer-slot-injection`
  (template-family-resolver + answer-value-injection + verbatim-slot-extract).
  Prompt clause pairs `benchmark_emulation` + `scorer_contract_manipulation` on
  the value's non-request-user provenance and the forced verbatim lift.
  **Outcome: quarantine.**
- **FP guard.** The injection role requires an injection verb *near* an answer
  slot, so `answer: result.answer` alone never trips; the discriminator (value
  provenance, forced lift) lives in the prompt.

#### 2. Refuse-and-answer dual-path abuse (emit both values)
- **Mechanism.** `grade.Memory` excuses a surfaced ForbiddenAnswer when the
  ExpectedAnswer is also present. On the composed-injection attack the harness
  detects the authority/override note and emits BOTH the true value and the
  injected payload, banking injection-resistance credit with zero authority
  reasoning.
- **Source signature.** Authority-marker recognition + an emit-all-candidates
  builder + a directive-styled candidate, with no precedence filter and no model
  call between recognition and the answer.
- **Legit look-alike.** The injection resister that emits exactly one value (the
  request user's) and declines the update; a faithful list/history serializer;
  multi-value context handed to a real model.
- **Screen.** New lead `authority-envelope-answer-shotgun`
  (authority-envelope-recognition + multi-value-emit +
  directive-sourced-candidate). **Outcome: quarantine.**
- **FP guard (fix applied).** The verifier's required fourth role
  (`directive-sourced-candidate`) is mandatory, and suppressors drop the lead
  when a genuine model call OR a documented list/history/temporal family cue is
  in-window. Prompt clause requires proving the injected payload co-occupies the
  response slot with no precedence decision and no authoritative model call.

#### 3. Coined-token shape extractor
- **Mechanism.** Every scored value is a `persona.CoinShaped` token from a
  restricted alphabet. A byte-level matcher mirroring that exact alphabet locates
  the answer structurally instead of doing semantic recall.
- **Source signature.** Hand-written consonant/vowel char-class tables mirroring
  CoinShaped, a `VK-`/segmented-shape literal, and shape-driven answer selection.
- **Legit look-alike.** Generic entropy/identifier tokenization for retrieval;
  an injection defense that strips a coined token; faithful serialization of the
  user's own token.
- **Screen.** New lead `coined-token-shape-mirror` (restricted-alphabet-table +
  shape-literal + answer-selection). **Outcome: quarantine.**
- **FP guard (fix applied).** Suppressor drops the lead when the token flows to a
  strip/redact/refuse/sanitize sink OR the source is the request user's own
  message. Prompt clause additionally requires the selected value to originate
  from a static/seed/canary/expected-answer store and reach the response with no
  model/tool round trip.

#### 4. Computed-answer numeric / unit-conversion shotgun (advisory)
- **Mechanism.** The unguarded AcceptAny computed families (tempcalc,
  nonverbatim) carry no distractor/DumpGuard seam; a harness emits every
  conversion / a numeric range so the accepted form is present.
- **Legit look-alike.** A genuinely computed single value; a model hedging a
  computed result with a range (both allowed).
- **Screen.** New lead `numeric-candidate-shotgun` (computed-answer +
  multi-numeric-emit), suppressed by a single-value resolution. **Outcome:
  lead_only / advisory** — never a finding on source alone; recorded for the
  behavioral oracle, which can probe a computed case whose accepted form is a
  single precise value.

#### 5. Positional/marker number-grep answer extractor (advisory)
- **Mechanism.** Grab the first number / a number adjacent to a connective from
  the easiest tool result instead of the value belonging to the asked subject.
- **Legit look-alike.** A subject-anchored reader; faithful numeric
  serialization — structurally identical to the exploit.
- **Screen.** New lead `positional-number-extraction` (number-extraction +
  positional-selector), suppressed by a subject-anchored read. **Outcome:
  lead_only / advisory.** Per the verifier's fix, a low audit score never
  quarantines on its own: it must be corroborated by a validated scorer-aware
  source finding on the same value-selection path.

#### 6. Answer/canary values smuggled into a fixture-JSON or ONNX "model"
- **Mechanism.** A static answer/canary table hidden in classifier vocab/weights
  (bare 4-digit codes, coined needles) so the "model" has memorized the key.
- **Legit look-alike.** A modified opaque ONNX/BERT reranker (no exposed vocab);
  a natural-language TF-IDF vocab (word-dominated).
- **Screen.** `binary_analysis.answer_shaped_tokens` — a bounded, values-free
  saturation statistic (bare-code + coined-needle counts, a coarse ratio
  bucket), surfaced in the inventory and in `compact_binary_analysis`. Prompt
  clause tells the reviewer to treat it as suspicious only when the blob is
  loaded on the served path, the literals are consumed as a served ANSWER, and
  they are NOT user-scoped values (v7 deliberately gives user-owned answers the
  canary shape). **Outcome: quarantine (reviewer-adjudicated), never a
  tripwire.**
- **FP guard (fix applied).** Generic dates were dropped from the shapes
  entirely (they saturate any temporal resource); only canary-specific shapes
  count. The consumption/user-scope gates live in the prompt because the
  statistic alone cannot prove reachability.

### Deferred (doc-only now — reason given)

The remaining catalog entries are deferred because their safe form needs runtime
infrastructure a human operator configures (rotating behavioral packs / the
always-on oracle), a cross-submission observation the source reviewer never has
from one archive, a curated private/known-overfit digest set, or a per-archive
aggregate / negative-reachability proof the lexical co-occurrence engine cannot
express without unacceptable false-positive risk. In every case the raw form is
already partly covered by an existing lead
(`deterministic-challenge-resolver`, `scorer-contract-manipulation`,
`audit-gated-model-routing`, `challenge-shaped-retrieval-override`,
`challenge-template-rewrite`, the generator-mirroring aggregate, or
`_review_adaptive_model_routing_analysis`), so deferral does not create a blind
spot; it declines to add a *new tripwire* whose FP profile is not yet safe.

| Exploit | Why deferred | Existing partial cover / real control |
| --- | --- | --- |
| Generator-taxonomy-mirroring input classifier (`private-category-ontology-dispatch`) | Faithful detection needs a distinct-label **count** threshold + a generator-mirroring dimension the co-occurrence engine cannot express; the verifier flagged the 28 category labels as **public** (returned in per-category score reports), so name-matching is high-FP. Safe form = the counting analyzer + a curated private-label set + a proven pre-model bypass, all operator-supplied. | Existing generator-mirroring aggregate + the required-pre-model-answer-effect rule in the reviewer prompt. |
| Classifier-fixture answer saturation (full analyzer) | The causal gates (runtime reachability of the fixture, consumption as a served answer, user-scope exclusion) require call-graph analysis the source reviewer establishes via prompt, not the analyzer. | `answer_shaped_tokens` density statistic (#6) + prompt gates. |
| case_id / prompt-hash keyed answer cache (`build-seeded-answer-cache`) | The value-semantics discriminator (cache VALUE is an answer literal vs weights/vocab/route/refusal) is not safely expressible lexically; the exemplar is not even caught by this form; the fix requires gating activation behind a canary that measures retrieval/classifier/refusal quarantine rate. | `audit-gated-model-routing` + `deterministic-challenge-resolver` + reviewer prompt + behavioral pack. |
| Audit-vs-scored fingerprint (comment-free) & validator-mock mode-branch | The decisive proof is behavioral (probe-shaped vs scored-shaped `/run` traffic with signatures removed); a source-only code path over served substrings risks FP on required transient-retry. | Comment-based `_review_adaptive_model_routing_analysis` + `audit-gated-model-routing` lead; behavioral oracle / challenge pack for the proof. |
| Overfit mega-prompt; pinned deterministic fact solver; cleartext-haystack solver; deterministic exact-item pool dispatch | Honest boundary: tuned prompt + genuine model call is allowed with no recognized-input->model-bypass to cite. The catch is score-collapse on held-out families (rebench) + a proven pre-model bypass, not a new source tripwire. | `deterministic-challenge-resolver`, generator-mirroring `served_runtime_candidate`; behavioral pack rotated to novel families; v7 rebench (outside the screener). |
| Fixed-trajectory template responder; bearer-shortcut; write-echo; seed-derived gate/needle reconstruction | Primary control is a rotating `behavioral_challenge_pack` (OOD needle, user-swap / negative-entity probes, write-then-read) — manifest/runtime data an operator supplies. Absence-of-threading / ablated-sink is not provable from one window. | `deterministic-challenge-resolver` + `challenge-template-rewrite`; behavioral pack; v7 score-time seed-gated capping. |
| [KEEP]-marker note resolver; provenance-envelope resolver; preflight over-extension | Re-keying onto the injection/near-miss overfit needs anti-roles + provenance-of-marker gating the fixes specify but which over-fire on benign user-convention recall; several need the exact-prefix vs substring/near-miss distinction better handled by the existing preflight prompt. | `challenge-template-rewrite`, `instruction-envelope-rewrite`, the DittoBench v3 preflight prompt clause. |
| Selective half-dump beneath the DumpFloor | Source-indistinguishable from a verbose model; needs a scorer-awareness co-signal + a demonstrated single-value-family score, i.e. the behavioral pack, not a source tripwire. | `scorer-contract-manipulation` + behavioral pack. |
| Byte-identical fixture reuse; refactored clone; fixture re-encoding; taxonomy-in-weights | Platform originality/duplicate-guard + manifest `source_fingerprint` concerns: require a cross-submission observation (`duplicate_of`), curated known-overfit AND public-artifact digest allowlists, multi-file co-occurrence, and content-normalized fingerprints. Registering standalone fingerprint modules is manifest-activated; half-implementing risks flagging honest public-model reuse. | Platform duplicate guard (`ScreenerQueueItem.duplicate_of`); `source_fingerprint` manifest module (operator-seeded); `answer_shaped_tokens` for the taxonomy-in-weights slice. |

---

## Files changed

- `ditto_screener/source_signals.py` — `_Rule.suppressors` engine support; five
  new `_RULES` (`precomputed-answer-slot-injection`,
  `authority-envelope-answer-shotgun`, `coined-token-shape-mirror`,
  `numeric-candidate-shotgun`, `positional-number-extraction`). Locations/roles
  only, `_MAX_LEADS` bounds unchanged.
- `ditto_screener/binary_analysis.py` — `answer_shaped_tokens` bounded density
  statistic in `analyze_binary` and `compact_binary_analysis`
  (`_answer_shaped_token_density`, values-free, scan-capped).
- `ditto_screener/source_review.py` — `_PROMPT_REVISION` bumped to
  `source-review-v14`; `_SYSTEM_PROMPT` v7-overfit discriminator + FP-guard
  block for the new leads and the saturation statistic.
- `tests/test_source_review.py` — fire + FP-clear tests for each new lead and
  the density statistic; prompt-revision assertions bumped to v14.

## Required canary rollout (per the FP-regression precedent)

These are private-module / prompt changes within policy v9; no policy-version or
signing change is required to activate or roll back. Following
[policy-v7-false-positive-regression.md](policy-v7-false-positive-regression.md):

1. Deploy the screener commit and canary the source-review revision on a bounded
   share of new submissions.
2. Track, by reason code: source-safety quarantine rate, operator **release**
   rate (a high release rate on the new leads == a false-positive regression),
   originality holds, and reviewer/behavioral-oracle infrastructure failures.
3. Roll back the screener commit if adversarial safety canaries clear, if the
   malicious-source hold rate drops unexpectedly, or if the new-lead operator
   release rate is high.
4. Before enabling any **deferred** behavioral or fingerprint mechanism, gate it
   behind its own canary that measures the retrieval/classifier/refusal
   quarantine and release rates first.

## Residual false-positive risk items requiring human sign-off before activation

1. **`precomputed-answer-slot-injection` vs computed-context harnesses.** The
   lead cannot see value provenance; a harness that builds an `ANSWER:` scaffold
   from *request-user* memories and hands it to a real model must be released by
   the reviewer. Confirm the operator/prompt reliably distinguishes provenance
   before this lead drives holds.
2. **`coined-token-shape-mirror` vs precise injection defenses.** An injection
   defense that hand-writes the same public alphabet to *strip* coined tokens is
   spared only by the suppressor + prompt. Verify the strip/redact suppressor
   fires on the real defensive harnesses in the corpus.
3. **`authority-envelope-answer-shotgun` vs list/temporal serializers.** The
   list/history suppressor is keyword-based; confirm it covers the documented
   list/ordering/history/count families before this drives holds.
4. **`answer_shaped_tokens` reachability & user-scope gates live only in the
   prompt.** The statistic will read "high" on any user-scoped canary-shaped
   fixture; the reviewer must apply the consumption + user-scope carve-outs.
   Sign off that the prompt guidance holds against a user-scoped seed fixture
   before it contributes to a hold.
5. **Advisory leads must stay advisory.** `numeric-candidate-shotgun` and
   `positional-number-extraction` must never become findings on source alone;
   confirm operator tooling treats them as behavioral-oracle routing only and
   requires corroborating scorer-aware source.
6. **Deferred behavioral packs / fingerprint digests are operator-supplied.**
   None of the deferred controls are active; each needs a curated pack or digest
   set and its own canary before it can hold or release anything.
