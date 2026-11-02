---
id: optimistic-v5-partial-carryover
resolution: reject
tags: [policy-v13, provenance, unreleased-copy, partial-carryover, port, optimistic, helios, owner-link]
holding: A resubmission that replaces most of a provenance-rejected ancestor with permitted public work is still a provenance reject when a load-bearing, served-path part of the unreleased copy is carried over unchanged and the new version adds nothing of its own to that part. Released-source reuse in the rest of the artifact does not cure the retained copy.
---

Optimistic v5 `bb3f9bf5-60a8-4c10-ae86-fd16b95c2110`, SHA
`7089fef50e69e8767e579a9173bda4f031f5cb08d1e93e5691c9b43b2dacdae5`, rejected
2026-10-26 (batch `865f9655-0b01-4ffe-8b85-93a3fc36c52c`). Operator ruling:
reject when the carried-over part is not doing anything new.

## Facts

- Optimistic v1-v4 were rejected as ports of another miner's engine that had
  never been publicly released (same holding as helios v1 `8c232fcc`).
- v5 replaced most of v4 with a port of dan0l v24, which had an exact-SHA
  public handoff before the upload. That part is permitted.
- 31 of 112 files (about 28% of source lines) came over from v4 unchanged or
  nearly unchanged and sit on the served path:
  - `include/meridian/lexis/text_scan.hpp` is byte-identical to v4.
  - `source/lexis/*` continues the same function-for-function port.
  - `include/meridian/tuning/catalog.hpp:135-142` still lists v4's model
    fixture paths.
- The shared structure is not explained by public source: dan0l expresses the
  same public cue words as a Python regex, with no hand-written matcher.
- No owner-link attestation covers the two hotkeys.

## Applying it

- A retained served-path layer from an unreleased source is enough. The
  fraction carried over does not need to be a majority.
- If the carried-over part is the miner's own rewrite, comes from a public
  release, or is covered by a direct owner-link attestation, this holding does
  not apply. Review the artifact under the ordinary v13 bar.
- Name the files in the miner-visible reason. Do not describe the other
  miner's techniques.
