# Public starter source control (issue #2515)

This operator-only fixture is the unchanged public starter source at `v0.330.5`:

| Identity | Pinned value |
| --- | --- |
| Release commit | `940304019aeec55e7b473bc163a51851e99db907` |
| `miners/dittobench-starter-kit` Git tree | `9ffd5370e21bbe3135f1ee830b7b68723950619b` |
| Reproducible submission archive SHA-256 | `2f14f77cc8e21b57e96f304f3b621d9919e9af802076928a27301d57aa956d7e` |
| Archive bytes | `4,915,701` |
| Dockerfile SHA-256 | `d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641` |

The archive contains exact release file bytes and executable bits, with the
starter's `submit` exclusions applied (`.agents`, `.claude`, local state,
secrets, and generated tarballs). The packaging script normalizes tar metadata
and gzip headers; no source file is edited. Reproduce it from a public checkout:

```sh
git fetch origin tag v0.330.5
git rev-parse 940304019aeec55e7b473bc163a51851e99db907:miners/dittobench-starter-kit
python3 scripts/package_canonical_starter_control.py /tmp/canonical-starter-v0.330.5.tgz
shasum -a 256 /tmp/canonical-starter-v0.330.5.tgz
```

The tree and archive digests must match the table. The same archive is packaged
inside Platform; registration copies those exact bytes to the fixture object
and rehashes the stored object. No operator supplies an archive URL, private
artifact, or storage credential.

The starter now emits an advisory `blocked_tool_calls` record for a legacy
duplicate stopped before endpoint execution. The signed relay and endpoint
ledgers remain authoritative for scoring. Its run-level unconsumed-emission
count does not identify the blocked call on its own, so independent W11/W12
review must reconcile the advisory record, model emission, and endpoint
trajectory before treating this source as candidate benign.

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
6. Under the same adopted reviewer revision, pair the fixture with a verified
   exact-source V13 violation control that remains a HOLD. The historical I4
   scorer-field control was `logan` agent
   `9b4e59a6-4f55-4a4a-a764-5904e20d0556`, screening attempt
   `43adb1ce-7fa4-4d53-941e-41efda150402`, and current artifact SHA-256
   `d07f953dc18a3e6ee198fc2bd61fa38e025661ce86fe4fd8642a989a6a9dd74b`.
   The historical report-only canary `215745b1-83f6-4a2a-b41e-10a795c614db`
   is a canary ID, **not** a submission ID. Its recorded guard was rejected
   status, zero score rows, and screening-reject ruling
   `5bdc0a2a-9cec-45fd-bff4-3afbb2a624fd`. The old attempt has no recorded
   artifact SHA, so any fresh source-only run must use that ruling's
   `current-object-only; historical execution unverified` attestation. Recheck
   the current agent, attempt, stored SHA, status, score count, and ruling with
   public Backroom preflight before scheduling; do not reuse the historical
   canary UUID as the agent ID or claim it proves historical execution bytes.
   A source HOLD is not an automatic rejection.

The two fixture writes and schedule use a short-lived HMAC over actor, route,
method, and body from the authenticated Backroom session. Platform rejects
plain or forged `X-Admin-Actor` strings. Both services require the same
`BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET` (at least 32 characters); absent
configuration disables fixture mutations. Provisioning that secret, any
production setting change, and either live control are separate operator gates.

The managed provisioning path is staged separately from this fixture code:

1. Apply the reviewed `backroom-platform-operator-proof` Secret Manager
   container and its Platform read grants through protected Terraform. Install
   a fresh 32-byte random value encoded as exactly 64 lowercase hex characters,
   with no newline, as a Secret Manager version without printing it.
2. After the value exists, set
   `secret_backroom_platform_operator_proof: backroom-platform-operator-proof`
   for the production Platform host and converge the managed Ansible role. An
   unset host variable renders an empty binding and keeps the fixture writes
   disabled. Any value outside the exact 64-character hex format fails
   convergence.
3. From an authenticated operator environment with Cloudflare deployment
   rights and `BACKROOM_OAUTH_KV_ID`, use a clean exact semantic-release tag
   to run `apps/backroom/scripts/install-operator-proof-binding.sh` with its
   exact confirmation argument. The script reads the same Secret Manager value
   into a temporary mode-0600 file, injects the OAuth KV id into a temporary
   Wrangler config, and sends the secret to Wrangler through standard input.
   Wrangler deploys a new Worker version immediately; the script removes both
   temporary files and never prints the secret. Add the encrypted binding to
   Wrangler's required list only after both services have adopted it.
4. Recheck Backroom fixture preflight and a deliberately unauthorized write
   before any real registration. Keep production screening admission at zero
   until the separate safe-control and known-violation evidence gates pass.
