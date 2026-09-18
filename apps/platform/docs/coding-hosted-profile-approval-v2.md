# Hosted-v2 profile approval document

`python -I -m ditto.coding_hosted_profile_approval` prepares and verifies the one
canonical approval document for the task-bound half of a hosted-v2 canary
bundle: the execution and grading profiles of one catalog index
(`services/dittobench-api/docs/coding-hosted-profiles-v2.md`). It is operator
tooling. Nothing runs it from Platform startup, CI, a worker, a validator or a
miner, and it changes no release, assignment, host or reward state.

Run it only on an owner-controlled machine. It never reads, accepts or writes a
private key.

## Approval model

The document has **no approval field**. `build` writes an unsigned draft and
prints `approved=false`. A draft stays unapproved however it is copied or
edited, because approval is not a value inside it.

A document is approved only by a detached curator signature over its exact
bytes. `verify` accepts only if that signature validates under the pinned
curator key. It uses the existing offline curator key and the signature format
of the private-v2 publication signing message
(`ditto/api_server/coding_private_v2_publication.py`):

- **Signed bytes:** `coding_canonical_json_bytes`, which is sorted keys, compact
  separators, UTF-8 and one trailing newline. There is no extra prefix or
  pre-hash. The closed `schema` value separates domains, so a publication
  message signed by the same key is never a valid approval document.
- **Signature:** a raw 64-byte Ed25519 signature in its own file.
- **Key identity:** `curator_signing_key_sha256` is the SHA-256 of the raw
  32-byte Ed25519 public key, as computed by `load_curator_signing_public_key`.
  It is inside the signed bytes and must also equal a pin supplied separately.
  For the registered `coding-private-v2-r2` curator key that pin is
  `aa7e1d820f2cfea52932c21629c0f51d3b1f9c2362b218e7a8759e32fd8b2220`.

## Build the draft

```text
python -I -m ditto.coding_hosted_profile_approval build \
  --request /ABS/PRIVATE/approval-request.json \
  --profile-request /ABS/PRIVATE/profile-request.json \
  --profiles /ABS/PRIVATE/PROFILE-HELPER-OUTPUT \
  --payload-authority /ABS/PRIVATE/payload/payload-authority.json \
  --registration /ABS/PRIVATE/private-v2-registration.json \
  --release-index /ABS/RELEASE/release.json \
  --native-approval /ABS/PRIVATE/native-controls-approval.json \
  --native-plan /ABS/PRIVATE/compatibility-plan.json \
  --native-summary /ABS/PRIVATE/NATIVE-MATRIX/summary.json \
  --native-provenance /ABS/PRIVATE/NATIVE-MATRIX/provenance.json \
  --curator-public-key /ABS/PRIVATE/curator-signing-public.pem \
  --output /ABS/PRIVATE/approval-document.json
```

Every path, including `--curator-public-key`, must be absolute.

- `--profiles` is the exact helper output directory: `execution-profile.json`,
  `grading-profile.json` and `receipt.json`, with no other files.
- `--profile-request` is the exact file given to the helper as `--request`. Its
  SHA-256 must equal the receipt's `request_sha256`. It must use the helper's
  exact field names; the builder rejects case variants that Go's decoder would
  accept.
- `--native-plan` is the private compatibility plan written by
  `coding_runtime/qualification/prepare.py` and run by the native controls. Its
  SHA-256 must equal the native approval's `plan_sha256`. It contains private
  driver arguments and corpus paths. The builder never prints them, and no
  rejection reason names them. The document binds only the plan digest.

The closed `dittobench-coding-hosted-profile-approval-request-v1` request rejects
unknown fields. It carries only reviewed choices and pins that come from
independent channels:

| Field | Source |
|---|---|
| `catalog_index`, `language` | The reviewed task and its runtime language |
| `source_revision` | The exact 40-hex revision of the release set and native controls |
| `registration_sha256` | The live registry row for the private-v2 release |
| `release_manifest_sha256` | The independently retained `release.json` SHA |
| `native_controls_approval_sha256` | The native controls approval the host consumed |
| `native_controls_provenance_sha256` | The retained native `provenance.json` SHA, recorded when the matrix finished |
| `native_controls_summary_sha256` | The retained native `summary.json` SHA, recorded when the matrix finished |
| `grader_contract_sha256` | `HostedGraderContractSHA256()` compiled at `source_revision` |
| `curator_signing_key_sha256` | The pinned curator key identity |
| `shadow_only`, `weight_eligible` | Explicit `true`, `false` |

## Builder checks

Any failure prints a fixed reason, exits 70 and writes nothing. JSON equality
checks are type-strict, so `true` never matches `1` and `1.0` never matches `1`.

1. **Profiles.** The receipt is canonical, has exactly the helper's fields and
   says `launch_checks_passed=true`, `approved=false`, `shadow_only=true` and
   `weight_eligible=false`. Both profiles are canonical and hash to the
   receipt's digests.
   - The execution profile has exactly the authoring-profile keys. Its budgets
     are four positive integers, with `wall_time_seconds <= 3600` and
     `workspace_tool_calls` equal to the candidate `MaxToolCalls`.
   - The grading profile has exactly the hosted-v2 keys, so a
     `test_manifest_sha256` is rejected. Its build and test commands are
     well-formed. Its test groups are `hidden` then `visible`, with trusted
     `dittobench-test-driver` commands, unique command IDs and counts from 1 to
     1,000,000. Timeouts are positive whole milliseconds within the Go bounds.
   - Both `resource_policy` objects are complete and exactly equal, because the
     helper derives them from one policy. The candidate `MaxPatchBytes` equals
     the receipt's `max_patch_bytes`.
   - Image, grader contract and grader bundle agree across the profiles and
     receipt. The grader contract equals the pin.
2. **Profile request.** The request hashes to the receipt's `request_sha256`
   and is the closed helper schema. Its catalog index, image, candidate and
   protected limits, combined disk bound, budgets, build command, test groups
   and execution timeout are exactly the values in the profiles, after the
   helper's millisecond-to-nanosecond conversion. The private sandbox values
   (`MemoryLimitBytes`, `ScratchLimitBytes`, `PidsLimit`, `CPUQuotaMillis`) come
   from the private resource profile, not the request, and are not checked here.
3. **Private-v2 identity.** The payload authority hashes to the receipt's file
   digest, and its self-digest recomputes to the receipt's `payload_sha256`.
   Exactly one task has the reviewed catalog index. Its task version,
   commitment and grader bundle match the receipt. The registration validates
   through `CodingPrivateV2RegistrationAuthority`, including its self-digest.
   It matches its pin and binds the same corpus release, private release,
   payload and catalog.
4. **Release set.** `release.json` matches its pin and its writer's exact
   encoding. Readiness fields are unedited and `source_revision` equals the
   reviewed revision. All four images carry their driver profiles and approval
   digests. The reviewed language's `image_ref` digest equals the profiles'
   `image_digest`.
5. **Native approval and plan.** The approval matches its pin and has the
   `dittobench-coding-native-controls-approval-v2` shape. Its revision, release
   set and four images equal the release index.
   - The plan hashes to the approval's `plan_sha256` and is `prepare.py`'s
     exact encoding. It is the closed plan schema at the reviewed revision, with
     two replicates and `production_api_approval=false`.
   - The task group is the task's `task_version_id` minus its memory-condition
     suffix. `coding_private_catalog_v2_compile.py` derives `task_version_id` as
     `{group_id}-{condition}`, for example `private-group-007-v0_none`.
   - The plan has exactly the four base/reference and visible/hidden cases for
     that group. Every one names the reviewed language.
   - For each phase, both roles run exactly the grading profile's driver argv
     and expected count for that group: `hidden` matches `test_groups[0]` and
     `visible` matches `test_groups[1]`.
6. **Native outputs.** `provenance.json` and `summary.json` each match their
   independent pin and are the native-bound outputs of `qualification/run.py` at
   the same revision. They carry the same `native_control_authority` for the
   approval and match its plan, helper and runner.
   - Provenance keeps `runtime_qualification` and `production_api_approval`
     false, and its inspected repository digests include the profile image.
   - The summary reports native and private controls passed, zero failed
     controls and equal repeats. `cases` equals the plan's case count, and
     `controls` equals twice that and the approval's `controls`. It has two
     replicates, all four languages, and every readiness flag still false.

On success `build` exclusively creates the mode-`0600` document in a protected
directory, fsyncs the file and its directory, and prints only `approved=false`
and `document_sha256`.

### Launch checks are the helper's attestation

The builder does not compile Go or read the private task objects, so it cannot
re-run the launch checks. `launch_checks_passed=true` is the helper's claim. The
builder binds it to the exact profile, request and payload bytes and checks the
structure above, but the claim itself is not independently verified here.

Before signing, the approver should re-run the helper built at `source_revision`
with the same `--profile-request`, payload authority and verified objects, into
a new directory. The helper's output is deterministic, so all three files must
be byte-identical to the reviewed profiles. In particular, `receipt.json` must
hash to the document's `profile_receipt_sha256`.

## Document fields

`dittobench-coding-hosted-profile-approval-v1` is a closed flat object with
these fields:
- `schema`, `coding_contract_version=2`, `catalog_index` and `language`;
- `source_revision`;
- task identity: `task_version_id`, `task_commitment_sha256`, `corpus_release_id`,
  `registration_sha256`, `private_release_sha256`, `payload_sha256` and
  `payload_authority_file_sha256`;
- profiles: `profile_receipt_sha256`, `execution_profile_sha256`,
  `grading_profile_sha256`, `grader_contract_sha256`, `grader_bundle_sha256`
  and `image_digest`;
- release set: `release_manifest_sha256` and `image_approval_sha256`;
- compatibility evidence: `native_controls_approval_sha256`,
  `native_controls_plan_sha256`, `native_controls_provenance_sha256` and
  `native_controls_summary_sha256`;
- `curator_signing_key_sha256`, `shadow_only=true` and `weight_eligible=false`.

### Where each bound value comes from

The builder hashes these input bytes itself. Each digest is also compared with
the pin or claim named here:

| Field | Recomputed from | Also equal to |
|---|---|---|
| `profile_receipt_sha256` | `receipt.json` | None |
| `execution_profile_sha256`, `grading_profile_sha256` | the profile files | receipt digests |
| `payload_authority_file_sha256` | `payload-authority.json` | receipt digest |
| `payload_sha256` | the payload authority's self-digest projection | receipt, registration |
| `registration_sha256` | the registration's self-digest (model validation) | request pin |
| `release_manifest_sha256` | `release.json` | request pin, native approval, native authority |
| `native_controls_approval_sha256` | the native approval file | request pin, native authority |
| `native_controls_plan_sha256` | the plan file | native approval, provenance |
| `native_controls_provenance_sha256`, `native_controls_summary_sha256` | the output files | request pins |
| `curator_signing_key_sha256` | the curator public key | request pin |

The builder does not recompute these values. It copies each one from an input
that is pinned or cross-checked, and compares it as listed:

| Field | Copied from | Compared with | Not verified here |
|---|---|---|---|
| `task_version_id`, `task_commitment_sha256` | payload authority task | receipt | The commitment is not recomputed from the catalog record |
| `corpus_release_id`, `private_release_sha256` | registration | receipt | None |
| `grader_contract_sha256` | grading profile | receipt, request pin | Not compiled; the pin must come from the helper at `source_revision` |
| `grader_bundle_sha256` | grading profile | receipt, payload task artifact | The bundle bytes are hashed by the helper, not here |
| `image_digest` | execution profile | grading profile, receipt, profile request, release `image_ref` | None |
| `image_approval_sha256` | `release.json` image entry | native approval image | The image `approval.json` bytes are not read |
| `catalog_index`, `language`, `source_revision` | request | receipt, plan, release set, native outputs | None |

## Offline signing

Review the digests. Then sign the exact document bytes on the offline signing
machine with the curator key, using the same step as the private-v2 publication
message:

```text
openssl pkeyutl -sign -rawin \
  -inkey <offline curator private key> \
  -in approval-document.json \
  -out approval-signature.bin
wc -c approval-signature.bin   # must be exactly 64
```

Ed25519 signatures are deterministic, so any conforming signer produces the
same 64 bytes. Do not re-encode, reformat or append to the document.

## Verify

```text
python -I -m ditto.coding_hosted_profile_approval verify \
  --document /ABS/PRIVATE/approval-document.json \
  --signature /ABS/PRIVATE/approval-signature.bin \
  --curator-public-key /ABS/PRIVATE/curator-signing-public.pem \
  --curator-signing-key-sha256 aa7e1d820f2cfea52932c21629c0f51d3b1f9c2362b218e7a8759e32fd8b2220
```

`verify` rejects (exit 70) unless all of these hold:
- the document is exact canonical bytes in the closed schema, with
  `shadow_only=true` and `weight_eligible=false`;
- the signature file is exactly 64 bytes;
- the public key path is absolute, and the key hashes to the pin and to the
  document's `curator_signing_key_sha256`;
- the signature verifies over the exact bytes.

A missing, empty or wrong-length signature is rejected, so an unsigned document
is never accepted. On success it prints `document_sha256` and
`signature_sha256`. It then prints the bound identities `catalog_index`,
`language`, `source_revision`, `task_version_id`, `corpus_release_id` and
`image_digest`, followed by every bound digest. `task_version_id` shows the
private group and memory condition. Treat the output with the same care as the
document.

## Not established here

- **Launch checks.** They are attested by the helper receipt and should be
  reproduced by re-running the helper, as described above.
- **Command IDs and timeouts.** The native controls run every case with the
  fixed `command_id=private-compatibility` and a 180000 ms timeout
  (`cmd/dittobench-coding-private-control`). The plan therefore carries neither
  value. The profile's test command IDs and timeouts are bound by the grading
  profile digest and checked against the Go bounds. No compatibility evidence
  shows that a shorter hosted timeout is enough.
- **Private sandbox values.** Memory, scratch, PID and CPU limits come from the
  private resource profile. Only the helper checks them against that profile.
- **Grader contract provenance.** The builder checks the grader contract against
  the reviewed pin. It cannot compile Go, so the pin must be taken from the
  helper built at `source_revision`.
- **Time-bound inputs.** The inference policy and budget profile are separate,
  re-issued at least daily and not covered by this signature.
- **Activation.** A valid signature does not install, import, select, assign,
  weight or reward anything. Hosted v2 still binds no test manifest, which
  weighted activation requires.
- **Revocation.** The document has no expiry. Superseding an approval means
  signing a new document and retiring the old digest out of band.
