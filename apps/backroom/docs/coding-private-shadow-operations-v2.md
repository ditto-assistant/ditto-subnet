# Coding private shadow operations v2

Status: proposed Backroom operations contract. This document does not expose a
new route, deploy a service, or authorize private task access.

Authorized operators need release-level visibility into private Coding shadow
operation without seeing private corpus material. The future Backroom surface
may expose:

```text
opaque release status and audit completeness
provider-profile and publication-receipt freshness
selection/quorum progress and infrastructure failures
aggregate p0-p4, monotone score, confidence, and evidence counts
candidate timeout/OOM/integrity counts
quarantine and retirement status
```

It MUST NOT expose task IDs, source repositories, issue text, memory records,
condition labels, hidden tests, patches, object keys, bucket names, endpoint
origins, credentials, wrapped keys, or unredacted provider receipts.

All values remain shadow diagnostics. Backroom may show `weight_eligible=false`
but may not toggle it. Release registration, selection, credential lifecycle,
private publication, and activation retain separate owner-authorized controls.
