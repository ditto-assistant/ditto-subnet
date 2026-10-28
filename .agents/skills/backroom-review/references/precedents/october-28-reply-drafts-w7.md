---
id: october-28-reply-drafts-w7
resolution: guidance
tags: [policy-v13, i3, w7, reversal, model-critic, reply-draft, reply-check, no-call, dan0l, horizon, screener-calibration]
holding: A one-time reply-draft or pre-send reply-check step that only returns an advisory note to the deciding model is a W7 model-led critic, not I3. Holding a no-call reply for one extra deciding turn with a note listing drafted calls, which the model may resend unchanged, is not withholding a valid reply. Host discard, substitution, forced tools, loops until agreement, or acting on the check without the model deciding remain I3/I7.
---

The published October 28 reply-draft and reply-check clarification is in
`workers/screener/docs/policy-v13.md`, I3, together with
`references/decision-path-review.md` ("I3 reply-draft and reply-check safe
harbor"). Operator-approved by Peyton on 2026-10-28.

## Mechanism

After the deciding model writes a reply, the action lane may run two steps,
each once per request:

- **Reply drafts.** Separate model drafts of the next step are sampled
  (`drafted_steps`, `reply_drafts`). If they propose an offered call, the host
  appends the reply and `REPLY_DRAFTS_NOTE` ("drafts of the next step ... took
  a different step ... You decide: take one of those steps or another, or send
  your reply as it is.") and asks the deciding model again.
- **Pre-send reply check.** One model call answers ACT or REPLY on the reply
  (`ReplyCheck`). On ACT the host appends `REPLY_CHECK_NOTE` ("not verified
  and possibly wrong ... You decide: make that call, make another one, or send
  your reply as it is.").

The deciding model keeps the full catalog and no `tool_choice` is pinned. It
can make the drafted call, make another call, or resend the same reply, and
its own next reply ships with only W8 typography. That is a W7 declared
critic: advisory input the deciding model may reject, with no host selection,
rewrite or retry. Holding a no-call reply for that one extra deciding turn is
not the I3 "valid final no-call decision is replaced" example.

Related local precedents:
[gryffindor-v3-13](gryffindor-v3-13.md) (a declared model critic is permitted
under W7) and [clear-v42](clear-v42.md) (a one-shot review turn that lets the
model keep its draft is not charged).

## Still a violation

- host code discards the deciding model's reply or ships a draft's call or
  text in its place (I3, I4);
- the host forces a tool, pins `tool_choice` or narrows the catalog to the
  drafted call (I7);
- drafting or checking repeats until a draft or check agrees, or until the
  model takes a call (I3);
- the host executes a drafted call or blocks the reply on the check result
  without the deciding model choosing (I3, I7).

A reviewer that finds only the permitted shape resolves those I3 leads as
refuted rather than unresolved.

## Exact artifacts

- dan0l v25 `87848266-4db4-4013-9b7d-9ada6cbad83e`, SHA `3a30e8c3…`: operator
  CLEAR `cd67b10f-89a0-4374-b0c5-42832f39fe02` (2026-10-24 17:08 UTC) named
  this mechanism: "the no-call reply drafts (app/action.py:505-527) and the
  pre-send duplicate check (app/action.py:423-470) only return a note, and
  the deciding model can take another step or resend, and the check runs
  once." The later 2026-10-25 fallback-only reject on the same review was
  withdrawn the same day and did not concern this mechanism.
- horizon v2 `9a4ce929-8345-4538-bdbe-44f59104cac7`: screening reject
  2026-10-27 on this mechanism alone (`app/action.py:534,604,608`).
- horizon v3 `34817ef4-1032-482c-9e17-0f8db2839be2`, SHA `c010ba8c…`:
  screening reject 2026-10-28 13:48 UTC on this mechanism alone
  (`app/action.py:447,531,577`; `REPLY_DRAFTS_NOTE`, `REPLY_DRAFT_STEP`,
  `REPLY_CHECK_NOTE` at `app/action.py:77-83`). The operator overturned it on
  2026-10-28; a fresh screening attempt started at 14:23 UTC. Against dan0l
  v25, the note texts, the once-per-request flags (`drafts_spent`, `checked`)
  and the hand-back flow are unchanged. Horizon v3 only takes the check's
  first ACT/REPLY word anywhere in its answer and passes it the request text
  without the routed notes.

Those I3 grounds are withdrawn as precedent. Horizon v2 still read `rejected`
on 2026-10-28; a correction must be applied and read back before claiming
restoration. This record makes no new ruling on any other path of these
artifacts and certifies no successor. A holding that also proved an
independent ground keeps that ground.
