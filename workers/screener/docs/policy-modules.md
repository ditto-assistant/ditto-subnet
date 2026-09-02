# Private policy modules

The policy boundary separates stable public-v8 enforcement from rotating
private triage. Daily rotations replace a protected JSON manifest and restart
the worker. They do not change `SCREENING_POLICY_VERSION`.

## Typed outcomes

| Outcome | Source | Public behavior |
| --- | --- | --- |
| `pass` | Stable core passed; selected private audit, if any, cleared | Signed `passed=true`; existing promotion applies |
| `deterministic_reject` | Objective stable-core archive, container contract, build, or health failure | Signed `passed=false`; terminal `rejected` |
| `retryable_infra` | Download, Docker host, policy feed/pack, or other infrastructure failure | Signed `passed=false` with the existing `screener error:` marker; retryable `screening_failed` |
| `pass_inconclusive` | A healthy submission exhausted a deterministic deep-review budget after cheap gates passed | Signed pass with public-safe budget evidence; score-first admission completes without a retry loop and remains eligible for deferred ATH review |
| `quarantine` | Private source review or behavioral audit needs review | Signed attempt-bound quarantine result; platform persists a non-scoreable hold |
| `inconclusive` | Selected private challenge could not yield usable evidence | No public verdict; bounded private journal entry; lease remains authoritative |

Only `deterministic_reject` is a terminal failure, and private modules cannot
emit it. Timing, score, relay, source, and response-shape observations are risk
signals, not proof that a harness did or did not causally use a model. Fast,
high-scoring submissions and a randomized control sample can both enter the
same rotating challenges, so fixed sleeps or dummy calls do not form the policy.
Insufficient behavioral-oracle round trips, wrong oracle answers, and timing
floors remain quarantine/escalation evidence; none is sole terminal proof.

## Manifest boundary

The strict manifest contains exactly `policy_version`, `rotation_id`, and
`modules`. At policy-v10 rollout, reissue the protected manifest with
`policy_version: 10` and a fresh rotation/digest before a v10 worker starts.
The worker intentionally refuses a v9 manifest; silently accepting it would
sign v10 verdicts under an unbound private-policy snapshot.

Supported module kinds are:

- `timing_relay_risk`: reads a bounded private aggregate feed and emits only a
  tripwire or retryable infrastructure outcome.
- `random_audit`: uses HMAC with the named secret seed environment variable to
  select a deterministic private control sample for that rotation.
- `source_fingerprint`: compares bounded canonical source/layout fingerprints
  and emits only a tripwire.
- `agentic_source_review`: gives GPT-5.6 Luna bounded
  read/list/literal-search/binary-analysis tools over the verified archive.
  Source is treated as adversarial data; the reviewer has no shell, edits,
  execution, web, external-directory, or secret tools. Binary analysis uses
  bounded in-process format parsers and is precomputed once for listed opaque
  files before the model starts; full tool results reuse the same cache. It
  never executes code, expands archive payloads, or loads external model data.
  Medium/high risk can only select quarantine or a behavioral audit.
  Exact SHA-256 provenance for named official starter-kit fixture/model files
  prevents unchanged binaries and seed data from being mistaken for suspicious
  static tables. Trust never extends to a changed path or derivative file.
  A bounded semantic-lead pass supplies location-only co-occurrence hints for
  challenge-shaped retrieval overrides, deterministic question resolvers,
  scorer-contract manipulation, request rewrites, user-isolation bugs,
  external build inputs, and C13 bench-family compiler fingerprints. Leads
  never decide policy or expose matched source; the reviewer must validate
  reachability and causal effect. C13 hits are search leads, never auto-bans.
  Policy v10 requires one signed decision for each I1-I7 invariant. I4 derived
  authority, I5 production generality, and I7 model tool planning remain
  independent even when a real model call occurs and the historical two limbs
  pass. Medium/high findings still select quarantine only; L2/L3 and the
  operator retain adjudication authority.
- `behavioral_challenge_pack`: runs bounded private `/run` requests only after a
  selector trips. It records response digests, elapsed time, and JSON keys, not
  private prompts or response bodies. An anomaly becomes quarantine and an
  unusable observation becomes inconclusive.

A challenge can opt into `require_model_call` and
`require_gateway_token`. The first requires at least one chat/responses call
to the isolated fake OpenAI-compatible gateway during that challenge. The
second additionally requires the harness response to carry the gateway's
per-container random output, establishing bounded request-to-response dataflow.
A missing call or missing dataflow token quarantines the attempt; it never
creates a deterministic rejection. Embedding-only traffic does not satisfy the
model-call check.

Behavioral challenges use the scorer's provider sequence. The first container
starts with the current `platform` selector and every historical base-URL alias
points at the same isolated broker. If that exact challenge is unusable and
records zero broker calls, the worker may restart the same immutable image once
with the historical `chutes` selector and repeat it within the original timeout.
A usable zero-call response remains policy evidence and is never retried.
`chutes` is only a compatibility adapter name here: it never selects the public
Chutes service and no provider credential enters the submitted container.

The always-on behavioral oracle sends its `/run` shaped like a scored tool case.
It carries a `user_id`, a `tools` catalog, and a `tool_endpoint` filled by the
gate from the reachable gateway, with the system prompt drawn from a pool that
includes the production scorer's own phrasing. This removes the audit-detection
signature seen in rejected submissions, which routed only requests with no tools,
no tool_endpoint, and no seeded memory to the real model while solving scored
requests deterministically. A tool case legitimately carries tools and an
endpoint and is not preceded by a seed, so an un-seeded tool-shaped `/run` is
ordinary scored traffic rather than a tell. The tool-call round trip is also the
natural path by which the gateway's nonce reaches the harness's second model
turn. The source reviewer additionally flags reachable code that classifies
requests as audit versus scored, by seed state, empty memory, or envelope
fields, to call the model only when watched.

Challenge packs and selectors remain protected operator inputs. Rotate request
shapes, identifiers, and audit sampling together; do not commit a production
pack or a known-source digest. Separate workers use separate Docker networks,
container names, gateway tokens, and state files, so horizontal concurrency
cannot mix audit evidence.

The worker logs the manifest digest and rotation ID at startup. Quarantine and
inconclusive journal records contain agent/attempt IDs, outcome, manifest
digest, and bounded public-safe evidence codes. The journal is created mode
`0600`; its parent is mode `0700`. Operators must rotate, retain, and inspect it
as private security data.

Source review uses `provider.zdr=true` and `data_collection=deny` on every
OpenRouter request. Tool output and step counts are bounded. The API key is read
from the mode-0400 file named by `SCREENER_SOURCE_REVIEW_API_KEY_FILE`; it is
never injected into a submitted container or written to the review journal.

The source-review cheating boundary and operator evidence requirements are
documented in [source-review-policy.md](source-review-policy.md).

Source-review holds use public-safe risk-domain reason codes. Private-challenge
risk, malicious-source risk, and exact-artifact originality risk are distinct;
raw categories, paths, prompts, and evidence stay private. A source-safe exact
duplicate must still be held by the originality guard and must not be relabeled
as private-challenge leakage.
