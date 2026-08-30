# Unified normal and coding miner capability (shadow contract)

## Status

This document defines the compatibility target for a future, **shadow-only**
coding-capable miner submission. It does not change the active upload protocol,
normal scorer, current Tool + Memory composite, validator weights, or
emissions. Coding contract v1 remains permanently `weight_eligible=false`.

The target is one submitted Docker build context and one screened image per
miner artifact. A miner may be normal-only or may advertise an additional
coding capability from that same image.

## One artifact, independent lanes

Every submitted artifact must continue to implement the normal DittoBench
protocol on port `8080`:

```text
GET  /health
POST /seed
POST /run
```

Those routes remain the only routes required for upload, normal screening, and
the active Tool + Memory evaluation. Existing normal-only agents therefore
remain valid without rebuilding or changing their score.

A coding-capable artifact additionally serves:

```text
GET  /coding/health
POST /coding/seed
POST /coding/run
```

The coding routes are additive. They are not a replacement for, extension of,
or alternate meaning of the normal routes. The mutable repository, hidden
tests, private catalog, validator credentials, authoritative patch, and score
evidence remain validator-owned; none belong in the submitted image.

There is one archive, not a normal-agent archive plus a second coding-agent
archive. This keeps the screened image digest, owner identity, normal score,
and coding capability attached to the same immutable submission. Public
practice packs are developer downloads, not required contents of the uploaded
archive.

## Optional coding-health advertisement

`GET /coding/health` is the sole initial discovery probe. It has no request
body and, for coding contract v1, returns a bounded JSON object such as:

```json
{
  "status": "ok",
  "supported_coding_contract_versions": [1],
  "capabilities": [
    "case_scoped_inference_v1",
    "coding_runner_tools_v1",
    "scoped_memory_seed_v1"
  ]
}
```

The known fields mean:

| Field | Rule |
| --- | --- |
| `status` | Must be the literal `"ok"`. |
| `supported_coding_contract_versions` | Non-empty, positive, duplicate-free versions; v1 requires `1`. |
| `capabilities` | Duplicate-free capability identifiers. Coding v1 requires all three values shown above. |

Unknown fields are non-authoritative and ignored for rolling compatibility.
Validators validate the known fields, normalize version and capability order
before persistence, and bind the resulting advertisement to the exact screened
artifact digest. A health response is an advertisement only; it is neither a
quality score nor evidence that the coding workflow is secure or correct.

Outcome handling is deliberately backwards-compatible:

- `404` means that the image is **normal-only**. It remains eligible for the
  normal pipeline and receives no coding score or penalty.
- A malformed response, another HTTP failure, or a transport failure yields no
  coding attestation. It must not change a completed normal score. Operators
  may retry the optional probe under a fresh, bounded certification attempt.
- A valid advertisement can enter coding certification, but only after the
  qualification gate below. It is not enough to receive a private task.

No caller may infer capability from an image name, source repository, a
miner-reported feature flag, or a successful public-practice run.

## Admission order

The future integration uses two independent score lanes and a strict order:

```text
submit one artifact
  -> normal build and /health screening
  -> normal Tool + Memory evaluation
  -> durable core-qualification decision for that exact artifact
  -> optional /coding/health probe
  -> coding canary certification for that exact artifact
  -> shadow coding-task admission
```

The core-qualification decision is derived from the normal Tool + Memory
pipeline. It must bind the same agent artifact and screened-image digests as
the optional coding attestation. A new upload, image rebuild, benchmark-version
change, expired certificate, or changed coding contract makes the coding
attestation ineligible until rechecked.

An agent can therefore be in exactly one of these externally useful states:

| State | Normal pipeline | Coding pipeline |
| --- | --- | --- |
| Normal-only | Eligible | Not probed or unsupported; no penalty. |
| Core-unqualified | Eligible under normal rules | Not admitted. |
| Core-qualified, coding-uncertified | Eligible | No private task yet. |
| Core-qualified, coding-certified | Eligible | Eligible for default-off shadow admission only. |

Coding certification is artifact-bound and must execute the existing public
canary through validator-owned workspace tools, a source-bound inference
capability, workspace freeze, transcript and patch evidence, and pristine
grading. It is a protocol-and-sandbox check, not a coding-quality leaderboard
score. Private post-commit tasks remain the only source of later coding-quality
evidence.

## Packaging and privacy rules

The unified image is packaged with the same build-context workflow as a normal
submission. It must retain a root `Dockerfile`, remain within the normal archive
limits, and start its normal health route on port `8080`. Coding support adds
only agent implementation and public protocol support.

It must never include:

- provider, wallet, Platform, validator, or workspace credentials;
- a private repository, hidden test, private memory profile, task corpus, or
  grader plan;
- a Docker socket, direct repository mount, or host-execution helper; or
- a miner-authored patch or test claim used as grading authority.

Validator-owned capabilities deliver the minimal per-case visible memory,
workspace operations, and locked inference route only after admission. The
validator derives the submitted patch from its own frozen workspace and grades
it in a separate pristine environment.

## Rollout boundary

The next implementation steps are intentionally narrow:

1. add a unified reference starter that serves the required normal routes and
   this optional coding-health advertisement from one image;
2. add an optional screener probe and artifact-bound capability attestation;
3. connect core qualification plus successful coding certification to
   default-off shadow admission; and
4. add local unified-package rehearsal and operator-visible diagnostics.

Each step remains score-neutral. A separate owner-approved activation, new
contract version, calibration evidence, and emissions-policy review are
required before coding can influence any reward.
