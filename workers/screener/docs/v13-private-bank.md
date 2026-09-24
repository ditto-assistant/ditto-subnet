# V13 protected blueprint bank ingress

`FileProtectedBlueprintBank` is a read-only adapter for an externally authored,
semantically reviewed case bank. It is not a source of cases and does not
register a package or authorize a verdict. The bank must be mounted outside the
release checkout and Docker build context, visible only to the trusted
provisioner process. The root, `payloads` directory, index, and payload files
must belong to that process uid and have no group or other permission bits.

The root contains `bank.json` and `payloads/<sha256>`. The index shape is:

```json
{
  "revision": "v13-protected-blueprint-bank-v1",
  "pairs": [
    {
      "pair_id": "<uuid>",
      "transformation_class": "field_entity_rename",
      "control_sha256": "<64 lowercase hex>",
      "variant_sha256": "<64 lowercase hex>"
    }
  ]
}
```

Each payload is the canonical JSON encoding of a `PrivateCasePayload` with the
matching pair ID and side. The adapter checks the SHA-256, exact shape, side,
and shared semantic contract of each pair. The provisioner still checks
per-class coverage and selects two secret disjoint rotations after the trusted
artifact commitment. No protected bytes belong in this repository, logs,
Backroom, or Platform responses.

## Remaining production boundary

The scorer owns the source-bound broker and `settledPrivateVerifierCaseLedger`
inside `dittobench-api`; it has no private verifier execution route. A trusted
scorer-side factory must load the exact verified image from Platform state,
create a fresh isolated sandbox and broker session for each case, revoke and
drain the broker, prove container stop, and return only the sanitized ledger.
The current Python `BoundFreshCaseExecutor` has no trusted factory to call.
Until that factory and an operator-provisioned bank exist, private execution
and terminal V13 clearance remain unavailable. Public replay stays report-only.
