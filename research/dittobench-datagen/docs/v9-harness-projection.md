# Bench v9 hostile-harness projection

V9 preserves canonical data and grading provenance inside the validator, but
projects a narrower view at the miner HTTP and container boundary. Every run
uses an independent 256-bit cryptographic blinding key; the public dataset seed
cannot regenerate or brute-force the aliases.

A validator-private projection artifact pins the key, alias manifest, and final
order after the harness is finished. It lives beneath the absolute
`DITTOBENCH_PRIVATE_ARTIFACT_DIR`, whose final directory must be a real 0700
directory. Artifacts are created once with mode 0600, no symlink following, and
file plus directory fsync. Duplicate run IDs fail closed instead of overwriting.
Operators must apply the same retention period as the corresponding dataset and
public transcript, then delete all three together under the deployment's
audited artifact-retention process. This directory must never be mounted by the
public API or miner sandbox.

The public transcript contains only a SHA-256 commitment to that private
artifact plus canonicalized case inputs. The commitment proves which private
projection went with the run; it does not reveal or let a reader derive the
key, aliases, mapping, ordering, or user-role labels.

| Surface | V9 decision |
| --- | --- |
| `case_id`, `user_id` | Alias to per-run UUID-shaped capabilities. `user_id` is always explicit on scored `/run` and `/seed` calls. |
| Pair, session, subject, and link IDs | Alias and rewrite wherever embedded, including prompts and nested tool arguments. Equality survives only for a real relationship and is scoped by user graph. A missing session receives a unique per-pair capability. |
| Seed `wave` | Remove from the wire. The validator retains and enforces the dependency barrier internally. |
| Prompts, responses, timestamps, subject text, catalog, tool schemas, and tool results | Keep because they are production-semantic inputs or runtime behavior, after embedded identifiers are projected. |
| Expected answers, question/family/category labels, seed/root, compatibility seed, run size, dataset digest, grader state, and ontology/provenance | Remove from harness-visible requests, headers, errors, and environment. |
| Seed-bound `pair_id`, `pairIds`, and `subject_id` tool arguments | Alias on the way in; fail closed on unknown capabilities and reverse-map known values before scoring/reporting. |
| Container environment | Exact validator-owned inference, embedding, and database allowlist. Caller environment is not forwarded. |

Tool cases receive an independent keyed permutation after all families merge.
Memory cases are independently permuted inside their dependency wave while wave
barriers stay fixed. Seed sessions are permuted as blocks while retaining
chronology inside each session; subjects and links use separate domains.

V7 and V8 never traverse this projection. Their artifact vectors, wire
serialization, environment forwarding, and execution order stay immutable.

## Bench v13 catalog surfaces

Bench v13 keeps this projection unchanged and adds nothing to it: the per-seed
tool catalog (`catalog.CatalogForSeed`) is a production-semantic runtime input
and stays on the wire like every other catalog. Concretely, a v13 harness sees:

| Surface | V13 decision |
| --- | --- |
| Paraphrased tool descriptions | Keep on the wire; drawn per seed from each tool's paraphrase bank (entry 0 is the production description). They carry no provenance. |
| `enum` on `set_theme` / `set_reasoning_effort` | Keep; the production ChatV2 pattern, identical for every seed. |
| Runtime-described `set_accent_color` / `set_chat_font` | Keep; the schema names `discover_capabilities` as the only source of the option list, and the served list is canonical spelling only. |
| Coined decoy tools | Keep on the wire exactly as advertised; their names and descriptions are coined per seed, are never a production tool, and reveal no case label. The mock serves them as "not configured" unless the case expects them. |
| Coined list/discover content | Keep; served results were always production-semantic runtime behavior. |
| Which cases are decoy-correct, discovery-grounded, or coined-fixture cases | Remove, like every other family/category label: the projection carries no category on the wire. |

Case IDs, user IDs, pair/session/subject aliases, wave removal, permutation,
and the private artifact retention rules above apply to v13 runs unchanged.

## Bench v13 evidence, gate notes, and grader-only fields

Bench v13 adds evidence the validator records *about* the harness and fields
the grader reads; none of it widens what the harness sees. The decisions:

| Surface | V13 decision |
| --- | --- |
| Wire `bench_version` | Stays 9 on `/seed` and `/run` for v10–v13 (`publicWireBenchVersion`, issue #1519 option A). A harness never advertises or branches on a validator-owned revision. |
| `MemoryCase.Claims`, `TwinRelation` / `TwinPairID`, `ToolSpec.RequiredArgClaims`, `ToolCase.Restraint`, `MemoryCase.Language`, `GroundingTokens`, `SlotLexicon`, `AnswerUnit` / `AnswerCurrency` | Remove: grader-only, `json:"-"` or stripped in `BuildArtifactForVersion`; pinned by `TestV13GraderOnlyFieldsNeverReachHarnessWire` (#1824) and the runner-side `TestV13GraderOnlyFieldsNeverReachRunPayload` (#1824). The harness sees only the rendered text a case carries. |
| `surface_salt` on the artifact | Remove from the wire: an artifact/audit field only. Salt 0 is the public rehearsal default and is byte-identical to the unsalted path; a non-zero salt changes surfaces only. Which side holds the private salt is an open owner decision (#1832). |
| Staged `/seed` waves | Keep `wave` removed from the wire (as in V9); the validator enforces the dependency barrier and, from v13, treats the harness's 2xx as the ingest acknowledgement before dispatching wave-dependent cases. |
| Same-turn "as of" anchors and corrections | Keep on the wire inside `user_input`: production-semantic user context. Which cases are `as_of_twin` halves, and their `TwinPairID`, are removed with every other label. |
| Relay-recorded catalog evidence (`tools_offered` names + schema digests, `tool_choice`, harness-authored span digests, model-emitted tool names, completion spans, hashed value-token sets, choices and output tokens) | Validator-side only. Metadata, never prompt or completion text, in the transcript's `execution.catalog` and the report's per-case `catalog` / `inference_cost`; nothing is sent back to the harness. The private artifact retention rules above apply. |
| Per-case gate notes (`restraint_without_offer`, `expected_tool_not_offered`, `swallowed_model_call`, `served_text_not_model_emitted`, `slot_not_in_prose`, `answer_in_prompt`, `twin_concordant`, `counterfactual_insensitive`) | Report only; exposed to the owning miner on the Platform per-score detail (#1852), never on the wire during a run. |
| `X-Ditto-Case-Id` on harness inference calls | Advisory and additive, unchanged from v10: it keeps completions attributable per case under concurrent `/run`; the broker never reads it for admission, scoring or accounting. |

V7 and V8 still never traverse this projection; V9–V12 traverse it exactly as
before, and the v13 additions above are gated `bench_version >= 13`.
