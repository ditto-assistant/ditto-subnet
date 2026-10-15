---
name: backroom-board-sweep
description: Run a rolling top-5 board sweep of the live SN118 leaderboard. Review the top five, every emission recipient and every unruled high scorer. Then re-read the board after each batch of rulings and review whatever rolls up, until the top five and the paid slots are all decided. Use for "rolling top 5", "sweep the board", "review every emission recipient", or sibling re-entry after a reject. The per-row bar, precedents and rulings court live in backroom-review.
---

# Rolling top-5 board sweep

This is the loop around `$backroom-review`. It does not restate the review bar:
load `.agents/skills/backroom-review/SKILL.md` and its `references/review-bar.md`,
`review-rules.md`, `decision-path-review.md` and `techniques.md` first. For
policy-v13 rows, apply the published checklist
(`workers/screener/docs/policy-v13.md`) end to end.

Use only the OAuth `sn118-backroom` MCP (`https://backroom.dittobench.ai/mcp`).
Call `get_backroom_access` first and require `backroom:read`,
`backroom:artifact:read` and `backroom:write`. The token expires mid-sweep, so
front-load artifact downloads.

## Loop

1. **Targets.** Call `get_leaderboard` (current bench) and
   `get_screening_review_queue`. Targets are:
   - the top five rows;
   - every `emissions.recipients` row;
   - any scored row at composite ≥ ~0.70 with no resolved ATH on that UUID.

   Check each with `get_ath_review`. A "copy review not found" means unruled. A
   prior keep on the hotkey or an ancestor is not a skip. Skip `banned` rows.
2. **Pin identity.** For each target, `get_screening_submission`: agent_id,
   name/version, full hotkey, artifact SHA-256, score count, status, policy
   version.
3. **Map the hotkey.** `search_submissions minerHotkey=<hk>` lists every scored
   sibling. The board shows one row per hotkey, so rejecting the visible row
   promotes the next scored sibling. Note those siblings now.
4. **Precedents first.** `search_ath_precedents` with the full hotkey or agent
   name. Results routinely overflow the tool output; parse the saved JSON with
   `python3 -c` instead of paging it. Find the latest ruled sibling and its
   charged mechanism.
5. **Diff against the ruled sibling.** `get_lineage_source_diff` (add `path` for
   one file). If the diff removes exactly the charged mechanism and the other
   files are byte-identical to an artifact whose other paths were already read,
   that usually settles a clear. If the charged files are byte-identical, the
   mechanism is still there; confirm reachability on this artifact's bytes.
6. **Download and read.** `get_screening_artifact`, then immediately
   `prepare_artifact.py --url ... --sha256 ... --output-dir <scratch>/art/<name>`.
   MCP source reads truncate around 600 characters per line, so never rest a
   decisive line on them. Run any Python that reads miner files with `python3 -I`.
   Trace the served path: entrypoint → request parsing → retrieval/routing →
   model → tool executor → final writer. Grep the generator
   (`research/dittobench-datagen`) for any suspicious phrase list before calling
   it benchmark-specific.
7. **Decide each row** under backroom-review: reject only with file:line you
   read yourself; clear when the model/tool path is intact; otherwise leave the
   row and report it.
8. **Write rulings** through the batched court (create upload, PUT, preview,
   execute with `APPLY ATH RULINGS BATCH`). Every item must be `ready`. A
   reject carries `evidence_references`; cite the generator lines too when the
   finding is an inverse of it. Clean rows get a `clear`, which records a
   hold-free attestation (see Gotchas).
9. **Roll.** Re-read `get_leaderboard`. If `ranked_count` did not drop after a
   reject, a sibling re-entered. Repeat from step 1 for every new top-5 or
   recipient row. Sibling chains on one hotkey can run several rounds; review
   each sibling on its own bytes.
10. **Close.** For every touched hotkey, run `search_submissions` again and check
    that every remaining `scored` row is either ruled or listed in the report.

## Gotchas

- **Clean rows are attested, not held.** `clear` on an unheld `scored` or
  `live` row with no review previews as `steps: ["attest"]` ("will clear without
  holding"): the row stays on the board and the crown cannot move. Never `open`
  a paid row just to record a clear. Keep clears in their own batch.
- **Split mixed batches.** The preview token binds the whole document. Drop the
  blocked items, upload again and re-preview rather than executing a partly
  blocked token.
- **Evaluating rows race the guard.** A row one score short of quorum can
  finalize between preview and execute; execute then fails it with `score count
  changed`. Re-read the score count, re-guard and re-preview; the finalized row
  may now be a paid board row.
- **Copy holds can be module splits.** A hold whose diff shows a few large
  files removed and many small files added is usually the same engine split
  into modules. Compare normalized line sets and long comment lines, check the
  public starter kits, the source-release gate (only emission-confirmed kings
  are public), direction and `get_owner_attestations`. Cross-owner reproduction
  of an unreleased engine with no owner link is a provenance reject (helios v1
  `8c232fcc`) with no I1–I8 finding.
- **Rejects on evaluating rows are allowed** (`["open","reject"]`, PR #2906);
  a bare `open` still needs scored or live.
- **Same name is not same miner.** Precedents mentioning a "delta v5" or
  "delta v7" can belong to a different hotkey. Match by UUID.
- **Safe harbors stay narrow.** A model-authorized repeat gate (a separate
  model decides, or the deciding model re-issues the call) is permitted. A
  blanket or phrase-gated success memo is I7 (Artemis v8 `2758f7a7`, test-v13 v3
  `eb13f315`). Timestamps kept with no recency recipe is the lets_637 clear
  (`8b3f0b57`); hidden timestamps plus an order disclaimer plus a change-wins
  recipe is I5 (Harry v14 `30d854e5`).

## Report

Table: agent, version/UUID, hotkey, SHA (short), policy, finding or precedent,
decision, decisive file:line. Then executed counts (batch ids), rows left and
why, and the final champion and paid slots from a fresh `get_leaderboard`.
