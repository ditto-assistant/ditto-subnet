# Public starter source control (issue #2515)

This operator-only fixture is the unchanged public starter source at `v0.325.3`:

| Identity | Pinned value |
| --- | --- |
| Release commit | `7b297ae96488cfa4790b8b9d9b788cc82c7d0442` |
| `miners/dittobench-starter-kit` Git tree | `7c8044a1cc77e342b5f58a24fe31c9a86cc4fd6b` |
| Reproducible submission archive SHA-256 | `6f0fb811e08558aab56f63dd13ea1d2e1462d85e711fd31b0362d0de5c611fef` |
| Archive bytes | `4,912,491` |
| Dockerfile SHA-256 | `d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641` |

The archive contains exact release file bytes and executable bits, with the
starter's `submit` exclusions applied (`.agents`, `.claude`, local state,
secrets, and generated tarballs). The packaging script normalizes tar metadata
and gzip headers; no source file is edited. Reproduce it from a public checkout:

```sh
git fetch origin tag v0.325.3
git rev-parse 7b297ae96488cfa4790b8b9d9b788cc82c7d0442:miners/dittobench-starter-kit
python3 scripts/package_canonical_starter_control.py /tmp/canonical-starter-v0.325.3.tgz
shasum -a 256 /tmp/canonical-starter-v0.325.3.tgz
```

The tree and archive digests must match the table. The same archive is packaged
inside Platform; registration copies those exact bytes to the fixture object
and rehashes the stored object. No operator supplies an archive URL, private
artifact, or storage credential.

## Control sequence

1. `get_canonical_starter_fixture_preflight` reads the pinned identity, stored
   object integrity, registration state, reviewer provenance, and readiness.
2. A signed-in Backroom operator calls `register_canonical_starter_fixture` with
   a new request UUID and active Hetzner node. This creates only a fixture row
   in `awaiting_review`; it creates no miner agent or screening attempt.
3. A **different** signed-in operator independently reviews the exact archive,
   built image, and served path against V13, including repeated external tool
   calls. Publish the review evidence in this public repository, calculate its
   SHA-256, and call `review_canonical_starter_fixture` with that digest, public
   evidence URL, exact archive and Dockerfile digests, and the reviewed build's
   image digest. Platform records an authenticated operator attestation; it
   does not fetch or validate the linked document. This marks a *candidate*
   only. A
   suspected or confirmed violation must not be attested as candidate benign.
4. After the fixture-capable screener release is adopted, a different operator
   calls `schedule_canonical_starter_fixture`. Platform rechecks the object,
   reviewer, node, and version before queuing one job. The worker inventories
   and builds the untrusted archive in isolation, then runs L1/L2 source review
   without serving it or loading private challenges. The worker records its
   own built image digest. Image IDs from separate builds need not match; the
   reviewer must explain any material build or served-path difference in the
   public evidence before treating the report as a safe control.
5. Read the result through `get_l2_report_canary`. Treat `certificate`, `hold`,
   and `inconclusive` as **report-only** evidence. They do not post a screening
   verdict, approve a miner, change a score, resolve quarantine, or open
   production admission. The unchanged direct-clear floor is 0.98.
6. Under the same adopted reviewer revision, use the existing
   `get_l2_report_canary_preflight` and `schedule_l2_report_canary` for the
   exact-source V13 I4 scorer-field control
   `215745b1-83f6-4a2a-b41e-10a795c614db`, subject to fresh attempt, SHA,
   status, score, and ruling guards. It must remain a HOLD before considering
   production review settings. A hold is not an automatic rejection.

The two fixture writes and schedule use a short-lived HMAC over actor, route,
method, and body from the authenticated Backroom session. Platform rejects
plain or forged `X-Admin-Actor` strings. Both services require the same
`BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET` (at least 32 characters); absent
configuration disables fixture mutations. Provisioning that secret, any
production setting change, and either live control are separate operator gates.
