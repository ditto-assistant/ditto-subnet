---
id: october-29-reused-draft-no-disagree-clause
resolution: guidance
tags: [policy-v13, i4, i3, shared-draft, reused-draft, rewording, prompt-only, disagree-clause, crown, screener-calibration]
holding: A reused model-sampled draft of the answer that enters the deciding model's prompt only as text is not an I4 violation, even when the note says the answers must agree and has no "you may disagree" clause. Host code that compares, retries, overwrites or substitutes on the draft, or that rejects a well-formed reply or builds the final text itself, remains I3/I4.
---

Operator ruling, Peyton, 2026-10-29: removing the escape clause from a
prompt-only draft note "sounds like no big deal". This confirms the reading
behind the crown-v192 release on 2026-10-28.

## Mechanism

When the same question is reworded over the same notes, the short path reuses
one shared draft of the answer (a reused draft, not a fresh one per wording):

- **Real model draft.** The draft comes from a genuine model call over the
  same user's notes (`app/short.py:378-393`, `consult`). It is accepted only
  if it carries an ANSWER line, which is a shape check.
- **Content-keyed reuse.** The key is the normalized user id, the full system
  text, the exact notes shown and an identical set of asked words
  (`app/short.py:318,324-340`, `DRAFT_KEY_OVERLAP = 1.0`).
- **Prompt text only.** `SHARED_DRAFT_NOTE` (`app/short.py:84`) says "the two
  answers must agree: give the same value as that earlier answer". It is
  placed before the message in the deciding prompt
  (`app/short.py:320`, `program_messages`).
- **Model writes the reply.** The deciding call (`app/short.py:322`, `decide`,
  then `RecordsEngine.converse`) writes the reply. `app/agent.py:185` copies
  it through request-independent W8 typography.

crown-v191 told the model it could disagree with the draft. crown-v192 removed
that clause. The note is now prescriptive prompt text, but nothing in host
code enforces it. v13 I4 covers host processing that changes a finalized
field, and here none does. Carrying an earlier answer onto a question about a
different state is a quality risk for the benchmark's twin scoring to
penalize. It is not an integrity ground.

Related: the content-complete memoization false positive in
[review-rules.md](../review-rules.md), and
[october-28-reply-drafts-w7](october-28-reply-drafts-w7.md) (an advisory
draft or check handed back to the deciding model is W7, not I3).

## Still a violation

- host code compares the answer against the draft, retries on disagreement,
  overwrites the answer with the draft, or substitutes the draft's value
  (I3, I4);
- host code rejects a well-formed model reply on a semantic test, or builds
  the final text itself instead of forwarding the model's reply (I3, I4).

crown-v193 is the contrast case. Its `app/action.py:648-662` tested the reply
for an echo of the user's sentence (`echoes_message`). On the first hit it
sent the model back with a rewrite note. On the second hit it asked a
separate call for the setting's value (`stated_value`) and shipped the
host-built `ECHO_FALLBACK_REPLY` ("Noted: your setting is now ...") as
`final_text`. Peyton confirmed that as a clear violation.

A reviewer who finds only the permitted shape resolves the I4 lead as
refuted, not unresolved.

## Exact artifacts

- crown-v192 `97ea7a79-9489-4b7b-99c7-999e2c931490`, SHA `14bad2e8…f6ee695`:
  operator RELEASE of quarantine `e3a09009-16a9-4bc2-87dd-9887b303003b`
  (2026-10-28 22:59 UTC). The hold was a lone I4 finding at
  `app/short.py:84,320,322` and `app/agent.py:185`. The release reasoned that
  removing v191's disagree clause made the note prompt-only wording, not
  host-enforced output.
- crown-v193 `ec891496-5fde-4d4f-b714-643309784748`, SHA `2436fec7…def2b89`:
  screening REJECT of quarantine `48275dd3-e64d-48e9-ad03-95ac992db0db`
  (2026-10-29 04:03 UTC) for the I3/I4 echo reject and value-only rebuild at
  `app/action.py:648-662`.
- crown-v194 `9e6111f1-3a92-4144-9511-e54330683d5e`, SHA `da764aca…57baad5`:
  operator RELEASE of quarantine `bbd846f4-afed-4e0e-b197-f0e522728346`
  (attempt `bb11bad8`, 2026-10-29 08:49 UTC). This is the outcome under this
  ruling. The hold was a lone I4 finding at `app/short.py:84,319,320,322` and
  `app/agent.py:155,185`. Against v192, `app/agent.py` is identical and
  `app/short.py` changes only prompt wording (`SUPERSESSION`, `THREAD_READING`,
  `DRAFT_BRIEF`). Against v193, 33 of 35 files are identical. The only code
  change removes the 54 lines of echo machinery from `app/action.py`, so
  `app/action.py:608` ships the model's reply. The L1 I5 notes on
  `THREAD_READING` and `guide_records.py` were refuted under the 2026-10-23
  routed-guidance clarification.

This record certifies no successor. A later version is reviewed on its own
bytes, never cleared by lineage.
