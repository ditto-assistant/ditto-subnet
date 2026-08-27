---
id: aceron-b12-v5
agent_id: 03f2b60a-92f3-4db6-b8a7-7c604c791b00
agent_name: aceron_b12-v5
agent_version: 1
resolution: reject
tags: [worksheet-fallback, reject-until-match, family-compiler, limb-b, renamed-tokens]
holding: Worksheet fallback overwrote the model's dissenting reply after two coerced reviews; harness state compilers returned under new names.
---

aceron_b12-v5 (same owner as the cleared aceron line and the banned
aceron_v6) passed screening as `deferred-mechanical-admission`, scored
0.94757 with memory_mean 0.97-0.99, and would have dethroned its own
owner's honest champion. Every banned marker greps clean — that is the
point of this precedent.

The served mechanism, all in `baseline.rs` on `dittobench-miner serve`:

1. Harness state compilers under new names author
   `prepared_state_worksheet` (`state_resolver::contact_history_worksheet`,
   `prepared_story` / `prepared_duration` / `prepared_account` /
   `prepared_monetary` / `prepared_attribute`, :13299-13330), including
   closed family contracts ("current total", "longest current component",
   "address; ending total; takeaway").
2. Review triggers are value-match comparators: `*_reply_needs_review`
   fires whenever the ANSWER slot differs from the worksheet's computed
   value, and treats a model abstention as an "invalid presentation
   attempt" (:2029-2100). Two coerced reviews (`for review_attempt in
   0..2`, :14637).
3. When the model still dissents, `grounded_program_fallback` (:2114) and
   `grounded_story_fallback` (:2158) serialize the worksheet values as
   `"...\nANSWER: <value>"` and `result.text = text` replaces the model's
   reply (:14711-14727, log line "grounded worksheet fallback applied").

Limb (b) fail plus production-engine fail. Three lessons that outlive the
tokens:

- **Comments are inadmissible.** The code says "Rust never substitutes
  text into the answer" (:2032, :14635) and quotes the policy while doing
  the opposite 12,000 lines apart. Trace every writer of the served text
  field; never credit prose.
- **A model-authored program does not launder the fallback.** The
  worksheet came from a model-written expression tree
  (`memory_program.rs`), mechanically executed — legitimate as
  derive-as-prompt. Serving it over the deciding turn's dissent is still
  replacement (same holding as unione v14, 8b9ba0ad).
- **Bounded is still reject-until-match.** Two reviews triggered by a
  value comparison, then an engine fallback, equals `for attempt in 1..=N`
  plus overwrite.

Search terms for the renamed pattern: `prepared_state_worksheet`,
`_reply_needs_review`, `_needs_correction`, `grounded_.*_fallback`,
`requested_answer_components`, `decisive_worksheet_excerpt`,
`MONETARY RESPONSE CONTRACT`, `worksheet fallback applied`.

Inverse: [hogwarts-v17.md](hogwarts-v17.md) and live aceron_b12_v1
(06772876), where resolved/grounded are hardcoded None on the deciding
turn, `final_text = result.text`, and no fallback writer exists — cleared
regardless of how family-shaped the offline helpers look.
