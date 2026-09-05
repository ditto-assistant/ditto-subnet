# Platform-only private v2 retrieval

Status: injected service primitive, default-off. No HTTP route, runtime startup,
production KMS adapter is added here. The separate
`coding-hosted-private-grants.md` layer supplies a database-backed grant store
for approved hosted attempts; neither primitive is configured at startup.

`PrivateV2InputRetriever` binds a registered release, canonical payload and
transport manifests, publication readback receipt and an independently trusted
curator verification key. It verifies their digest linkage and curator signature
before accepting any object grant. A registration alone does not authorize use:
the grant store must consult the current release lifecycle and attempt phase on
every check, including after provider download and immediately before returning
plaintext. Retired or quarantined releases must yield no active grant.

The curator public-key file is selected by trusted operator configuration, not
by the receipt or a candidate. The hardened publication loader verifies that
key and signature before the retriever checks the registered object linkage.

The grant selects one catalog index, explicit asset roles, evaluation and attempt,
audience and expiry. The caller cannot supply an object key, object digest or URL.
The retriever derives the exact v2 Hippius key from the authorized transport
manifest, downloads the complete bounded ciphertext, checks its length and
SHA-256, requests external unwrap bound to that grant/object/AAD, verifies the
unwrap response binding, performs AES-GCM authentication, and verifies plaintext
length and SHA-256. There is no local ciphertext fallback or bucket enumeration.
The entire read, including grant lookups and unwrap, has a 30-second wall-time
limit. Grant expiry is rechecked before unwrap and before plaintext return.

Authoring grants cannot request grader bundles. Grading grants require a frozen
patch digest and the separate Platform grading audience. The durable grant
store must verify that this digest represents an actual committed patch freeze;
providing a syntactically valid digest is not proof of that state transition.

Only trusted Platform components may instantiate this retriever or receive its
plaintext result. It is not a miner- or validator-facing download API. Before
materialization, the execution adapter must validate the returned role-specific
schema and construct the approved candidate-visible projection. Private catalog
records and grader assets must never be forwarded wholesale to the candidate.

The existing reader adapter defaults to the v1 key namespace. A v2 service must
explicitly select `object_namespace="v2"`; each instance rejects the other
namespace. Existing endpoint pinning, redirect rejection and byte bounds remain.

Errors use fixed messages without private object bytes or provider details.
Retrieval failure is not candidate failure and must not be scored as a failed
patch. No score, worker activation, weight or emissions path is introduced.

Tests use synthetic ciphertext, real AES-GCM and curator signatures, and an
injected unwrap implementation. They prove local validation behavior, not live
Hippius access, production wrapping-key custody or deployed grant revocation.
