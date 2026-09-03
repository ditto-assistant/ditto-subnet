# Hippius Coding canary operator runner

## Status

This layer makes the phase-6 canary contract callable by one explicit operator
without adding a route or scheduler. It remains disabled unless
`DITTO_CODING_HIPPIUS_CANARY_ENABLED=true`, every protected input validates,
and the exact confirmation is supplied. Merge alone performs no provider,
database, unwrap, executor, deployment, score, weight, or emission operation.

The operator is deliberately not part of the Platform API lifespan. It is a
one-shot command run from exact merged and deployed source after the protected
synthetic transport, helper executables, evidence runtime, and database
migration are independently ready.

## Exact source and one-shot fence

The protected plan contains the expected 40-character merged source SHA. A
separate mode-`0600` deployed-source record contains the revision observed by
the deployment workflow. The command also resolves `git rev-parse HEAD` from a
tracked-clean checkout. All three values must agree.

The helper root and receipt directory must be absolute, owned by the effective
user, and mode `0700`. A nonblocking mode-`0600` file lock permits only one
operator process in that root. The receipt path must be new; the operator
refuses to overwrite or follow a symlink.

## Protected helper boundary

Platform keeps no private unwrap key and does not implement a second candidate
runtime. Instead, three distinct absolute executables are configured:

- unwrap helper;
- authoring helper; and
- pristine-grading helper.

Each executable must be an owner-controlled regular file with exact mode
`0500` or `0700`. They run without a shell, with stderr discarded, a fixed
minimal environment, bounded canonical JSON on stdin/stdout, a deadline-bounded
timeout, and separate owner-only working directories beneath the helper root.
No Hippius, Platform, cloud, or model credential is forwarded in the child
environment. A helper may use only its independently provisioned OS identity or
local protected service boundary.

The unwrap request carries the already reviewed ticket-bound wrapped-key
authority and returns only the matching 32-byte data key plus request digest
and expiry. The authoring helper receives issue, model-visible runtime policy,
budgets, runner plan, and execution digests, but no grader plan. The grading
helper receives the frozen submission, its digest, the protected grader plan,
and resource profile, but no issue or other authoring material. Both outcomes
must echo the execution and task digests; grading must also echo the frozen
submission digest and prove pristine execution.

The repository does not provide these protected helper implementations. They
must adapt the already reviewed external unwrap boundary and phase-separated
Coding supervisor. Test helpers are synthetic fixtures and are forbidden in a
live run.

## Protected inputs

When enabled, the operator requires these absolute paths:

```text
DITTO_CODING_HIPPIUS_CANARY_PLAN_PATH
DITTO_CODING_HIPPIUS_CANARY_DEPLOYED_SOURCE_PATH
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_MANIFEST_PATH
DITTO_CODING_HIPPIUS_PRIVATE_INPUT_PUBLICATION_RECEIPT_PATH
DITTO_CODING_HIPPIUS_CURATOR_PUBLIC_KEY_PATH
DITTO_CODING_HIPPIUS_CANARY_UNWRAP_EXECUTABLE
DITTO_CODING_HIPPIUS_CANARY_AUTHORING_EXECUTABLE
DITTO_CODING_HIPPIUS_CANARY_GRADING_EXECUTABLE
DITTO_CODING_HIPPIUS_CANARY_HELPER_WORK_ROOT
```

The helper root must already contain distinct mode-`0700` `unwrap`,
`authoring`, and `grading` directories. The optional helper timeout is bounded
between 1 and 7,200 seconds:

```text
DITTO_CODING_HIPPIUS_CANARY_HELPER_TIMEOUT_SECONDS
```

The phase-3 reader settings, phase-5 evidence settings, Postgres settings, and
dedicated provider credentials remain environment/Secret Manager inputs. No
credential, private key, task text, grader content, transcript, patch, object
key, or URL is accepted as a command-line argument.

The plan is canonical, owner-only, and names a reserved
`hippius-synthetic-canary-*` release, one validator/ticket, claim generation
`1`, the registered encrypted transport, and the exact expected plaintext
record digest. It remains protected because it contains raw ticket and
validator authority even though it contains no provider credential.

## Invocation

After the lower stack is merged, released, deployed, migrated, and configured,
the only command entry point is:

```bash
cd apps/platform
uv run python scripts/run_hippius_shadow_canary.py \
  --confirm "RUN HIPPIUS CODING SHADOW CANARY" \
  --output /protected/new-hippius-canary-receipt.json
```

Success prints only the canary-run and receipt-payload SHA-256 values. The
mode-`0600` receipt itself remains the review authority. A failure prints only
a redacted class-level reason and never falls back to GCS, a public bucket, a
different validator, or a second synthetic record.

## Remaining activation boundary

This PR creates no plan, synthetic release, helper executable, unwrap key,
credential value, provider object, deployment configuration, or live receipt.
It does not enable Platform's ordinary Coding worker or catalog, and it does
not apply Terraform, converge Ansible, restart a service, score a result, or
change weights or emissions.

Phase 6 becomes operationally complete only when the owner reviews a ready
receipt produced by this command from exact merged and deployed source. Phase 7
remains a separate settlement-bound three-validator review.
