# Miner communication precedents

Peyton's miner DMs can contain policy clarifications, exact-mechanism
assurances and reversals missing from the live court reporter. Check them
before applying a ruling. Use the logged-in Discord web UI through
[discord-miner-triage](../../discord-miner-triage/SKILL.md); this check grants
no authority to send a message, react or change a court record.

1. Search the live court and the existing `references/precedents/` reporter by
   exact UUID, full hotkey, miner and mechanism. Resolve the miner conversation
   from supplied artifact identities or verified surrounding messages; a
   Discord display name alone does not bind a submission.
2. Read Peyton's statements and the miner question or proposal they answer.
   Capture author, timestamp, message permalink, exact affected identity when
   available, policy era, permitted mechanism and stated conditions. A short
   “yes” is meaningful only with its surrounding question.
3. Distinguish a policy assurance, a promised investigation and an executed
   court reversal. Check current Backroom state for the last one. Retain
   superseded holdings as history and name the later correction explicitly.
4. Compare the proposed charge to the same mechanism and applicable policy.
   Explain any material difference using current source and configuration.
   Do not silently reverse a prior assurance, reapply a withdrawn charge, or
   turn an approval of one technique into clearance of unrelated paths.
   If the conflict cannot be resolved from verified evidence, leave that
   ruling unapplied and report the precise conflict to Peyton.
5. Update or add a narrowly scoped entry in the existing
   `references/precedents/` directory using its frontmatter format so
   `scripts/search-precedents.py` can find it. Record a concise holding,
   conditions, primary message link and verified court reference separately.
   Use `resolution: guidance` for communication-only clarifications or a
   reported disposition whose exact court action was not verified. Say what
   was checked; do not invent a `clear` or infer an absent court record from
   an ATH lookup when the original action may have been in quarantine.
   Record only policy-relevant facts, not private chat transcripts, personal
   details, credentials, private challenge data or unfixed evasion details.

If a relevant conversation is unavailable, record the gap rather than
claiming no precedent exists. Continue independent inspection while access
or a material conflict is resolved. In the sweep report identify which
communications were checked and how they affected the decision.
