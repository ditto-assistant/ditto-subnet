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

Files are opened relative to validated directory descriptors with no symlink
following. Nonregular files, including FIFOs, are refused without waiting for a
writer. Permissions and metadata are checked on the opened descriptor; changing
a path after open cannot substitute different bytes into the read.

## Remaining production boundary

The current `v13_private_runtime.py` supplies the isolated private runtime and
`run_v13_private_replay.py` consumes an already registered group. The bank loader
does not provision that group, a reviewed benign image, protected case bytes,
an independent enrolled runner, or a sealed publisher. Its hash and shape
checks cannot establish semantic equivalence between the two sides of a pair.

The canonical public starter fixture is a candidate control, not an approved
benign image. Follow `docs/canonical-starter-source-control.md` for its exact
source/image review; do not create a fake scored miner or fabricate a screening
attempt to satisfy the private control registry's existing guards. All private
results and public replay remain report-only until the complete verification
profile and its activation prerequisites are met.
