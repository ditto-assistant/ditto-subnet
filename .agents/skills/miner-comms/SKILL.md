---
name: miner-comms
description: >
  Draft SN118 miner-facing replies from Discord screenshots, DMs, copy-hold
  appeals, owner-link questions, champion/dethrone confusion, and confirmation-
  seed complaints. Investigate live Backroom state first, then produce a
  paste-ready message. Use when the user runs /miner-comms, asks for a miner
  reply, pastes a Discord screenshot, or says "gimme a reply for this miner".
---

# Miner comms

Operator skill. Investigate, then write a message the miner can act on.
Mutations stay on `$backroom-review`. This skill only drafts.

## 1. Resolve

From the screenshot or paste, keep exact agent UUID, name, version, hotkey,
and the claim. Rank, truncated keys, and "my agent" are leads. Call
`get_backroom_access`, then `get_screening_submission` / `get_leaderboard` /
`get_ath_review` / `get_owner_attestations` as needed.

Do not invent a UUID. If two IDs are in the thread, treat each as its own row
(`identity-uuid-not-sha`).

## 2. Investigate

Read live state, not the screenshot's clock. Champion, seed badge, and hold
status move. Cite SHA, status, attestation IDs, and Jaccard numbers from
Backroom.

Use `$backroom-review` for copy/ATH facts and `$mine` for practice/upload
facts. Topic answers live in
[references/topics.md](references/topics.md). Point at
[`docs/OWNER-LINKS.md`](../../../docs/OWNER-LINKS.md) and
[`services/dittobench-api/docs/seed-and-scoring.md`](../../../services/dittobench-api/docs/seed-and-scoring.md)
instead of restating those docs.

Inspect-only unless the user also asked to clear, reject, or hold.

## 3. Reply

Give the user a paste-ready Discord block. Voice:

- Helpful and specific. Lead with the answer.
- Name the exact UUIDs they already posted.
- Do not dump internal review-bar class letters, private challenge values, or
  other miners' payment coldkeys they did not already see.
- Separate "what we found" from "what you can do next".
- If an operator action is still needed, say so **above** the paste block,
  not inside it.

Do not promise a clear, un-ban, or extra emission slot.
