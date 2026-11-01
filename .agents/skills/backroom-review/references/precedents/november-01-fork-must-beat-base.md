---
id: november-01-fork-must-beat-base
resolution: policy
tags: [fork, released-base, emissions, sn-302, beast, dan0l, shared-seed, miner-communication]
holding: A fork of another owner's publicly released agent is allowed but earns nothing until it beats that base on at least 3 shared confirmation seeds through the crown gate's paired test. This applies to tail slots as well as the crown. Same-owner successors are exempt. Do not reject a fork for being a fork; the emission gate withholds it.
---

## What happened

"beast" v1 (`c0a0d917`, hotkey `5CBD7DW3…`) was uploaded 8 minutes after
dan0l v29's public release with a one-sentence prompt delta, and it cleared
and could earn as if it were its own work. Peyton approved SN-302 on
2026-11-01: a fork must beat its base.

## Ruling

- Fork detection uses the released-base court basis and the distinctive-content
  index. The base is the other-owner released agent with the largest shared
  distinctive content.
- `fork_must_beat_base` (`off` / `shadow` / `enforce`) is on the emission
  eligibility policy. It rolled out `shadow` first, and `get_fork_gate` lists
  who would lose emissions.
- Board and Backroom state: `fork_unproven`, "fork of X — must beat base: N/3
  confirmations, lead ±Δ".

## Using this precedent

A copy-hold ruling still decides whether an artifact is allowed at all. This
gate decides only whether an allowed fork is paid. For a "why am I not
earning" question, quote the agent's `reward_eligibility` fork fields or its
`get_fork_gate` row. Never promise a date.
