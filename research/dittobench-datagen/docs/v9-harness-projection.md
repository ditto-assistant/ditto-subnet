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
