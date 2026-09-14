# Fixed graph retest: complete cost accounting, with missing costs explicit

Status: accounting preparation only; no new full QA result or monetary total
is asserted here. The rerun reuses the prepared memories. That can make
**incremental seeding/dreaming inference zero**, but it does not make the
historical cost of constructing those memories zero.

| Phase | Evidence / accounting status before launch |
| --- | --- |
| New reader calls | Passive receipts planned; provider-measured charge pending |
| New judge calls, including retries | Passive receipts planned; provider-measured charge pending |
| New query/tool embeddings | Vertex; charge unknown unless separately measured, estimates labeled separately |
| Original fixture embeddings and seeding | Historical charge unknown; pre-embedded data reuse is not evidence of free creation |
| Original extraction/dreaming/refinement/labeling | Historical charge unknown; no receipts found in inspected logs or stored token-usage column |
| This rerun's reused seed/dream work | Zero incremental inference only if no extraction/dream/label work is replayed; verify the final phase journal |
| Graph refresh/local database operations | No OpenRouter inference charge if detached labeling is suppressed; local infrastructure excluded from provider-cost subtotal |
| Full lifecycle and preparation amortization | Unestablished while historical preparation and other categories are missing |

## Cost categories and units

Report each question's reader charges and judge charges separately, plus query
and tool embeddings where evidence is available. Also report original fixture
embedding/ingestion, extraction, refinement, cluster labeling and preparation
retries. Database copy/graph SQL and local compute have no OpenRouter charge;
that is not a claim that infrastructure or engineering time is free.

OpenRouter's [usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
returns `usage.cost` in responses, including the final streaming usage event.
Its [generation metadata endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)
allows historical lookup by saved generation ID. These are provider-reported
account charges, not the backend's price-table estimates. OpenRouter says its
[credit/API prices are USD-denominated](https://openrouter.ai/docs/faq).
Keep `usage.cost` and generation `total_cost` as alternative evidence for the
same OpenRouter account charge, not additive charges. For ordinary non-BYOK
requests, do not add upstream cost again. BYOK differs: separately preserve
`upstream_inference_cost`, classify it as a provider-reported BYOK estimate,
and report it beside the OpenRouter charge. OpenRouter's
[activity documentation](https://openrouter.ai/docs/cookbook/administration/activity-export)
describes external BYOK spend as market-rate estimates that may not reflect
vendor discounts. BYOK/vendor invoices, taxes and credit-purchase fees require
separate evidence; this is not a complete cash-invoice reconciliation.

Use exact decimal arithmetic. Missing, malformed, inaccessible or delayed
cost records remain unknown. An explicit provider zero is distinct from no
receipt. Deduplicate cumulative stream events by generation ID; do not add
each update as another request. Retain retries and failed-case receipts, not
only the successful final checkpoint. Never use an account-wide balance delta
to attribute spend to this campaign on a shared key.
The passive journal does not prove a response's finality: a canceled stream can
leave a partial cumulative usage cost. Response-only amounts therefore remain
**provisional observed subtotals**, not finalized charges. After calls drain,
reconcile every response-only generation through GET metadata before certifying
saved-generation charge coverage. A final charge may differ from the earlier
observation; keep both without adding them or requiring equality.

## What historical evidence establishes

The [preparation inventory](results/2026-09-14-preparation-cost-inventory.json)
hashes **72 retained preparation logs, manifests and audit receipts** from the
original full-memory workspace. None contains a recognizable OpenRouter
generation ID or a JSON cost/token-usage field. These receipts establish
preparation state/provenance, not billed amounts. This bounded inventory does
not prove no other historical receipt exists. A separate read-only check of
the new private copy at `2026-09-14T12:34:14.654951Z` found **zero nonnull
`memory_pairs.token_usage` values among 124,366 rows**. The copy's pre-refresh
semantic snapshot still matched the original `01c6a7070cba4e1455894c6da07132dcbba1b5cbc37c2c58c9f26db4adb3d055`;
this column provides no historical preparation charge receipts either.

The historical reader reports retain generation IDs under
`per_case.data.provider_responses` (`ID`, `Model`, `Provider`). The old judge's
`chatCompletion` returns only response content, discarding the provider ID and
usage; therefore reader recovery alone cannot establish judge/preparation cost.
The backend's previous Luna price fallback was explicitly invalid and is not
reused as a dollar estimate. Persisted pair/subject/vector counts are not billable
token counts or call counts: retries, batching, caching and reused embeddings
prevent that conversion without further evidence.

On September 14, the approved legacy ADC and standard ADC paths both failed
direct refresh with `RefreshError`; gcloud CLI authentication also required
reauthentication. No provider key was retrieved and no historical generation
lookup succeeded. No credentials are included in artifacts. This is an
authentication blocker for recovery, not evidence of zero charges.
No full fixed-graph reader/judge run has launched at this accounting handoff.

### Resumed authentication and real-provider compatibility check

After the user's reauthentication, local-key access and a historical generation
GET succeeded. That supersedes the authentication blocker above. The first
verified Luna receipt has `is_byok=true`, OpenRouter `total_cost=0`, and
`upstream_inference_cost=0.0017014`. It is **not free inference**. The auditor
now separates reconciled OpenRouter charges, BYOK upstream estimates, and an
explicitly estimated selected-generation sum. That sum remains unknown when
any referenced receipt, route classification or BYOK upstream estimate is
missing; full lifecycle cost stays unknown regardless.

The receipt resolves requested `openai/gpt-5.6-luna` to
`openai/gpt-5.6-luna-20260709`. The public
[OpenRouter model catalog](https://openrouter.ai/api/v1/models) independently
returns that exact `canonical_slug` for that exact requested ID. The auditor
allows only this explicit mapping, records both names and rejects arbitrary
dated/prefix variants. A private sanitized receipt/catalog artifact is retained
at `.tmp/cost-recovery/real-receipt-and-model-catalog.json`, SHA-256
`eaee286d3def2d64aeec4eed5eae81d5f9a61134804aeda5293eb06d35761cb1`.

A bounded analytics-schema GET returned HTTP 403 with the local inference key.
The [official analytics guide](https://openrouter.ai/docs/cookbook/administration/analytics-cost-control)
requires a management key. No account-spend query or account-balance attribution
was performed. Historical seed/dream spend is still unestablished; successful
reader metadata recovery does not fill that gap.

### Recovered historical reader-only costs

Both saved reader sets are now fully reconciled by generation ID: 1,281 OFF and
1,312 ON, with no missing metadata receipts. Every saved generation is BYOK.
These are the original fully seeded/dreamed, isolated LongMemEval-S Luna-medium
conditions, not new fixed-graph results. The [frozen result report](ditto-full-memory-results-2026-09-13.md)
records the exact OFF `20260913-054910-1b5755609b3ea95660fdba289e6a747adb8c5dae`
and ON `20260913-055526-1b5755609b3ea95660fdba289e6a747adb8c5dae` runs.

| Saved reader set | OpenRouter charges | BYOK upstream estimate | Estimated mean / median / p95 per question |
| --- | ---: | ---: | --- |
| OFF, 500 questions | $0 | $1.86599670 | $0.0037319934 / $0.00218895 / $0.00706200 |
| ON, 500 questions | $0 | $1.56381271 | $0.00312762542 / $0.002258765 / $0.00756430 |

The BYOK values are provider-reported upstream estimates, not independently
verified vendor invoices. Query statistics sum saved reader generations per
question; p95 uses nearest rank. Original preparation, historical judge calls,
embeddings and failed attempts without saved IDs remain excluded/unknown.
Neither row is an all-in evaluation cost. The exclusive-create recovery
artifacts are `.tmp/cost-recovery/historical-{off,on}-cost-recovery.json`, SHA-256
OFF `b6f35c67fd59bd89dd9ae7602ff7541c6abf9d1e19259db13bd8e59230c77563`
and ON `a2c759afa536b37bbeed2fdbddd19a57b97e6386626a2dfd335e6da927ab9b83`.
Their reaggregated per-question artifacts retain these input hashes plus the
updated auditor hash; no original source/report/checkpoint was modified.

The first actual judge receipt from the new run also resolves
`google/gemini-3.1-flash-lite` to catalog-confirmed
`google/gemini-3.1-flash-lite-20260507`. That exact mapping is now allowed, with
all other variants still rejected. Its non-BYOK OpenRouter charge is
$0.00007575, separately recorded from reader BYOK estimates. This one receipt
verifies compatibility, not complete new-run cost coverage.

### Invalid first fixed attempt: preserve expenditure, never a QA result

The first fixed-source `5ad4eee2` attempt is **invalid**: its copied fixture
lacked the `memory_pairs.metadata` schema column required by native hydration,
so seeded contexts were empty. It was stopped; the cancellation wiring did not
drain on SIGINT and the process ultimately exited 137 after forced termination.
Its retained successful query/judge status fields do not make this a valid
memory evaluation. No accuracy score is reported from this attempt.

Closed evidence contains 384 checkpoint rows and 3,532 usage-journal events,
deduplicated to 1,959 captured generations: 1,575 reader and 384 judge calls
across 391 represented questions. The journal includes calls for cases absent
from the completed checkpoint. The checkpoint SHA-256 is
`5101d8966fad878f3ca2386ffada7df0eb6f2cc18e7006a4af6d12e467829b52`;
the journal SHA-256 is
`b7adeb0e07d0c580a340f011845b576394040bb9c56babf0d5feaa154c3e15a0`.

Recovery uses a clearly marked `invalid_attempt` cost-only wrapper containing
only case/model/provider-ID references, never copied scores, questions, answers
or a fabricated complete run. `--attempt-classification invalid_attempt`
retains the attributed charges but suppresses valid-run per-question cost
mean/median/p95 fields. Charge this expenditure to the overall campaign, not
the later valid rerun's per-question reader mean. Calls charged before any
generation ID was observed remain an explicit unknown. The future valid run
must use corrected schema and fail-closed native hydration/seed-context gates;
no failed attempt is silently replaced in the accounting record.

GET recovery completed for all 1,959 captured generations, with zero missing
metadata/route/upstream-estimate records. It records $0.03843100 in OpenRouter
judge charges and $0 in OpenRouter reader charges, plus $1.02672237 in reported
BYOK reader estimates: **$1.06515337 estimated captured invalid-attempt spend**.
Two provider receipts are marked canceled and remain included. The private
`invalid-5ad-cost-recovery.json` SHA-256 is
`15dfefc434ada33931d7515b790289681f5db77c2cd51ef404d8d326e5ef43f5`;
its generation-receipt journal SHA-256 is
`7b220d3846de55d5b600714772b19de3a28fa88f8d38c3d6ef5549d5609c2e5b`.
This is not an invoice or a valid-query mean; preparation, embeddings and
charges before capture still prevent a complete lifecycle total.

### Additional acceptance gate for the corrected full run

Run the independent `audit_backend_run.py` with
`--require-hydration-preflight` for the corrected condition. It requires
`lme_hydration_preflight=native-hydration-v1`, an exact checked-user count
matching the unique selected fixture users (500 for this dataset), and a
positive integer `seed_pair_count` in every case. With graph retrieval ON it
also requires positive discovery calls, zero failures, a true discovery-complete
flag and consistent zero reason counters. Historical audit behavior stays
unchanged unless this flag is explicitly requested; no old result is rewritten.
The flag proves the recorded acceptance checks, not unqualified generalization
or absence of other retrieval limitations.

## New capture and aggregation

The planned passive backend journal is
`<checkpoint>.provider-usage.jsonl`, with case/attempt/stage attribution and
generation ID, model/provider, tokens, explicit usage presence and
`cost_status` / `cost_credits`. Reader and judge prompts, routing, temperatures,
tools and answer selection must remain unchanged. The final per-case report
also retains receipts, while the journal preserves calls for failed cases.
Calls that fail before a generation ID arrives still have unknown charge
coverage; a saved-generation-complete report must not claim every attempt was
captured. Query/tool embeddings use Vertex and are a separate category, not
automatically covered by OpenRouter generation receipts.

The offline [cost auditor](../../integrations/longmemeval/audit_openrouter_costs.py)
reads the final report and optional journal, deduplicates cumulative events,
rejects cross-case/model attribution conflicts and emits per-case/stage sums.
Early blank-model events may acquire their model from later events for the same
case/stage/attempt/generation; conflicting nonempty models and unresolved final
identities fail validation. Duplicate saved receipts fail before journal merging.
It separately reports captured judge subtotal and the question denominator.
Mean, median and nearest-rank p95 of captured generation cost per question are
emitted only when every referenced generation has a reconciled GET charge; they
still exclude uncaptured attempts and lifecycle costs. Both missing-cost and
response-cost-only journal generations remain eligible for metadata recovery.
Only already reconciled generation receipts can skip GET lookup. Provisional
amounts stay separate when a lookup fails and never unlock priced-complete stats.
`--fetch` performs only GET requests to the fixed OpenRouter generation
metadata endpoint using an environment key; redirects are rejected and
authentication/rate-limit failures stop further lookup. It never sends prompts
or performs inference. Output files are exclusive-create and mode 0600.

```sh
python3 services/dittobench-api/integrations/longmemeval/audit_openrouter_costs.py \
  --report /private/final-report.json \
  --journal /private/checkpoint.provider-usage.jsonl \
  --output /new/private/cost-audit.json
# Add --fetch only after authorized local-key access works; never paste the key.
```

## Per-question and overall presentation

For an independently verified historical preparation total `P`, an explicitly
equal-amortization view is `P / 500` per question. This is an allocation policy,
not measured question-specific ingestion spend. Add each question's measured
reader/judge/embedding charges separately. The lifecycle total is
`P + sum(reader + judge + embedding + attributed retries)`; the incremental
rerun total excludes already-paid preparation but includes any newly executed
refresh/embedding/labeling work. Do not double-count failed attempts already
represented by generation receipts or count the same preparation afresh in
every matched arm's combined campaign total.

Until `P` and other missing categories are established, report the captured
subtotal, its coverage and unknown components. Full lifecycle cost and its
per-question amortization remain **null/unknown**, not the captured subtotal
under a broader label. No inference should be repeated merely to manufacture
historical cost receipts.

Reproduction: run `inventory_preparation_cost_evidence.py --root /private/.tmp/lme
--output /new/private/inventory.json`. Run `python3 -m unittest -q
test_audit_openrouter_costs` from the integration directory. This work changes
research accounting only, not production scoring or billing records.
