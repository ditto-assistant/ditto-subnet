# V13 known-benign control attestations

The generation registry records a candidate control and its exact screened
image. That row alone has `recorded_unverified` status. `X-Admin-Actor` is an
audit label carried under a shared Platform bearer and cannot identify an
independent reviewer.

The Backroom `attest_v13_known_benign` write tool signs the current Google OAuth
session subject with a dedicated `DITTO_V13_BENIGN_ATTESTATION_SECRET`. Configure
the same random secret in Backroom and Platform, separately from the shared
admin token and session secret. Without it the endpoint returns 503. Never
expose it to the browser, client code, logs, or a public worker binding.

Each assertion expires after 120 seconds and binds the approval UUID and review
evidence digest. Platform verifies the signature and stores an immutable
reviewer subject, email, assertion digest, and database timestamp. Two distinct
Google subjects and emails must attest the same approval before its read-only
provenance status becomes `two_person_authenticated`. The receipt digest binds
both immutable rows. The original approval row and any existing generation
group remain unverified until this separate proof is checked.

This is an **identity and quorum gate**, not a semantic verdict. Reviewers must
inspect the exact source, image, runtime behavior, and evidence independently.
Future seed issuers must re-read the Platform provenance, verify its receipt and
that both database attestation times precede the generation start, and apply
the remaining protected challenge gates. No attestation issues a seed, changes
a miner hold, or alters emissions.
