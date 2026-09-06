# Hosted v2 harness interoperability

Status: native request/client/projector and reference-harness support. No worker,
inference grant issuer, private release, scoring or reward path is activated.

## Protocol boundaries

`HostedSeedRequest` and `HostedRunRequest` are distinct Go types with version 2.
They retain the bounded candidate-visible fields and raw memory representation.
The legacy validators and clients remain v1-only. In hosted requests `ticket_id`
is the opaque evaluation UUID and `case_id` is the opaque attempt UUID. They are
not private task/group IDs or v1 ticket leases. The v2 run is bounded to one hour.

The Rust starter advertises both versions. A seeded case records its version;
seed replay and run claiming require the same version, memory identity and
profile capability. The workspace client sends that version on every typed tool
call. A v1 seed cannot be consumed by a v2 run or overwritten with a v2 seed.
Memory indexing/retrieval strategy is unchanged and no external embedding
capability or provider credential is introduced.

`ProjectHosted` validates the version-neutral raw memory artifact and constructs
the native v2 request before transport. `DeliverHosted` performs one bounded
delivery, verifies the acknowledgement and rejects late success. Its projection
is copied on access and cannot be serialized as diagnostic JSON.

`HostedHTTPHarnessClient` requires an explicitly authorized transport; it rejects
the ambient/default proxy transport, disables redirects and shares the bounded
response parser. It requires HTTP 200, validates health/capability support and
seed/run identity, and rejects missing/null acknowledgement fields. Run responses
remain advisory: only the trusted workspace and grader determine the patch and
outcome. A successful health response is not certification of a private run.

The existing public practice remains non-authoritative. The legacy nine-task
source used by the compatibility test is not restored as the distributed public
practice pack; that pack remains the ten-task repository-hosted release.

## Verification and remaining integration

The scripted E2E command now runs both the existing v1 path and the real Rust
starter against a native v2 Go workspace. The v2 test sends a relevant raw memory,
checks idempotent seeding, observes native tool events and a real file edit, freezes
the patch, verifies pristine replay and rejects a second run after state cleanup.
It uses public fixtures and a scripted model—not live inference, a private grader,
Hippius/KMS, or scoreable evidence.

The Platform worker still must provide screened-harness isolation, verified
input projections, scoped inference grants, source-bound workspace publication,
quiescence/revocation, committed freeze, native grading and sealed terminal
evidence. This PR does not make the v1 worker eligible for private v2 data.
