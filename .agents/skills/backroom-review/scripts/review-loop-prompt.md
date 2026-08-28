# SN118 backroom review — one bounded fire (scheduled loop)

Standing authority for this ONE fire: you are delegated operator authority to
inspect and RESOLVE items in the SN118 review courts via the `sn118-backroom`
MCP — `release`/`reject` screening quarantines and `clear`/`reject` ATH
holds — each with a specific miner-visible reason. This authority is supplied
by the saved scheduled-task prompt per the backroom-review skill.

## Hard rules

- Do NOT rescreen anything.
- Leave rows that are miner-requested holds or family-dedupe holds (their
  hold reason says so) — they are not integrity cases.
- Leave stranded holds (a pending review whose `agent_status` moved;
  `resolve_ath_review` answers 409 "agent is no longer held") — they need
  platform-side unsticking, not a retry.
- Skip `banned` rows. Park `evaluating` / `waiting_validator` rows for a
  later fire.
- Leave rows with missing source, mixed evidence, or infrastructure failure
  unchanged and list them in the report with what would resolve them.
- A quarantine release needs decisive lines read in source; a reject needs
  the same. Automated findings are leads, not verdicts. High score alone is
  a lead, not evidence.

## Process (bounded: max 5 quarantines + 5 ATH rows per fire)

1. `get_backroom_access` (must be full scope).
2. `list_screening_quarantines status=active sort=oldest` and
   `get_screening_review_queue` (generation=all).
3. Read the adjudication standards before deciding:
   `.agents/skills/backroom-review/references/review-rules.md` and
   `references/review-bar.md` (policy v10: I1–I7 independent; I3/I4/I5/I7
   fatal; a known-false-positive class releases with a cited refutation).
4. Per row: `get_screening_quarantine_context(s)` (note lineage, duplicates,
   shadow_review), then read every decisive cited line with
   `read_screening_source_file` / `search_screening_source`, and
   `search_ath_precedents` for the pattern. For a no-finding
   inconclusive/repeatedly-inconclusive quarantine, do the full manual
   served-path review: entrypoint -> parsing -> retrieval -> model -> tools
   -> graded slot.
5. Resolve per item with `resolve_screening_quarantine` /
   `resolve_ath_review`: the reason names the policy category, the minimal
   file:line set actually read, and the cited precedent. No challenge values.
6. Re-read each resolved row to verify the write landed. A timeout is
   ambiguous — verify before retrying.

## Report

End with a compact table (agent, UUID, court, policy/finding or precedent,
decision, decisive path), executed counts, and rows left for the next fire.
If there is nothing actionable, reply with one line saying so.
