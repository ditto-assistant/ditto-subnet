# One bounded native TLS recovery

The corrected backend invocation at source
`c274a3f96e8176577043de95b048f1f2f3e9272d` finished all 500 workers but judged
only 495 cases. Five queries returned exactly
`agent loop: remote error: tls: bad record MAC`:
`71017276`, `gpt4_e072b769`, `gpt4_4929293a`, `af082822`, `0db4c65d`.
Selection is exclusively by this transport failure, never by answer correctness.
The original report remains incomplete; its partial accuracy is not a completed
benchmark result. Its successful 495 answers remain usable for bounded recovery.

Native hydration preflight passed for all 500 users. The graph audit recorded
3,744 calls, 31,140 candidates and zero failures. Prepared memory before/after
was identical at
`8af521b88d155c4c6e81befe89495b7e74328f65a734b91fdd45348a1a8a8023`.
Private PostgreSQL logs contained zero ERROR/FATAL/PANIC entries during this
invocation. Unlike the earlier schema-invalid attempt, these are transport
failures with successful native memory preparation.

The native error path writes failures to the final report, not the checkpoint.
Therefore the 495-row checkpoint alone cannot establish zero query failures;
the complete native report is authoritative. Preserve both files and the first
provider journal unchanged.

## Frozen one-invocation recovery

`integrations/longmemeval/resume_fixed_backend_tls.py` imports the backend's
committed launcher verifier only from the verified frozen checkout. It checks
exact source, binary, dataset, manifest, literal config and argv, then additionally
checks the pinned original report/checkpoint/journal hashes, exactly 495
successful checkpoint rows matching the report, and exactly the five TLS
failures above as their complement. It rejects changed answer rows, duplicate
IDs, unjudged rows, missing seed context, changed graph/snapshot condition, or
any other retry cause.

The wrapper exclusively creates a one-invocation claim, fresh launch receipt,
and byte-identical copy of the 495-row checkpoint. It never copies the old
provider journal: the new journal contains only recovery calls. Existing paths
or the persistent claim refuse another execution, including after a crash.
No automated retries follow this recovery. Further failures require a report
and explicit new direction.

From the task's `ditto-subnet` clone, using the reviewed sibling backend spec:

```sh
python3 services/dittobench-api/integrations/longmemeval/resume_fixed_backend_tls.py \
  --spec ../backend/.tmp/lme-fixed/qa-corrected-on-retry5-spec.json
python3 services/dittobench-api/integrations/longmemeval/resume_fixed_backend_tls.py \
  --spec ../backend/.tmp/lme-fixed/qa-corrected-on-retry5-spec.json --execute
```

The spec retains the full-500 graph-ON command, Luna medium, Flash Lite judge,
question-date clock and concurrency eight. Its backend binary SHA256 is
`09b02ebb47094eff7729366920c5aa84fcff80acdd655cc1aef413584894e332`.
Only output/checkpoint/log/receipt paths change to `qa-corrected-on-retry5-*`.
The existing native checkpoint guards bind the resumed answers to condition
`5b5c15226d6c2a81323d44948c40bf395f2e0c20f4b6d392a0c7cc51fd40a4c1`.
No backend source or provider policy is changed.

## Accounting and evidence boundary

The native implementation repeats `PrepareCaseContext` for all 500 cases before
skipping completed answer workers. Thus this recovery pays for only five new
reader/judge case executions, but repeats 500 query-embedding/retrieval
preparations. Embedding costs are unmeasured and must not be reported as zero.
No fixture seeding, extraction, dreaming, label generation or graph rebuild is
replayed.

The final native report combines the original 495 answers with five new results.
Each invocation's graph audit covers only its own execution; preserve both
audits rather than attributing the first 495 contexts to the recovery audit.
Latency aggregates from the recovery represent only newly queried cases, not
all 500. Combine the two provider journals offline and reconcile generation IDs,
including charges for the first five failed calls. Keep the earlier schema-invalid
attempt and historical preparation costs separate. Missing historical costs and
existing training overlap still prevent all-in-cost or clean-held-out claims.

After execution require all 500 final rows judged, no query/judge errors, valid
per-case hydrated seed context, zero graph failures, unchanged prepared snapshot,
unchanged original artifact hashes, and the independent strict audit. This
document records the approved method, not a claim that recovery has succeeded.

Focused guard tests:

```sh
cd services/dittobench-api/integrations/longmemeval
python3 -m unittest test_resume_fixed_backend_tls -v
```
