---
name: discord-miner-triage
description: Triage recent Ditto SN118 Discord channel messages and miner DMs in the logged-in web browser, including recurring check-ins and overnight catch-up. Route exact review, scoring, PR, and moderation work; verify outcomes before replying. Use for a channel/DM sweep, not a single pasted miner reply.
---

# Discord miner triage

Work from the logged-in **Discord web tab** using the browser interface available
in the current environment. This skill is independent of a particular agent
product or browser API. Do not require the native Discord app, desktop-screen
automation, a Discord bot token, or direct Discord REST calls. If the tab was
closed, reopen Discord in the browser first. If login is unavailable after
that, report it once and wait for access to be restored.

This skill organizes an authorized triage run; it does not grant permission to
send messages, delete content, change production state, or schedule future runs.
Use the current user request or saved schedule for those boundaries. Read
[miner-comms](../miner-comms/SKILL.md) for an individual miner reply and
[backroom-review](../backroom-review/SKILL.md) before any source-review or ATH
decision. Repository PR work follows [github](../github/SKILL.md).

## Establish the window

1. Read the main Ditto channel from the last completed cursor through now.
   For an overnight catch-up, start at the requested time window. Do not treat
   unread markers as a complete history.
2. Inspect recent miner DMs, including already-read conversations that changed
   within the window. Distinguish miners from unrelated DMs. Read surrounding
   context before acting on a clipped message or screenshot.
3. Capture the message permalink, author, timestamp, exact claim/request, and
   any supplied agent UUID, version, hotkey, artifact digest, issue, or PR.
   A name, rank, truncated key, or screenshot row is only a lead.
4. Compare against the prior checkpoint and live external state. Do not
   redispatch a settled request, resend a reply, or repeat a guarded write for
   the same artifact unless new evidence, a new version, or a state change
   warrants it.

## Route and finish each actionable item

| Item | Action |
| --- | --- |
| Routine miner question | Verify the exact live state through Backroom or repository evidence. Give the answer and next action in plain language. When authorized to send, identify yourself as Peyton's assistant; do not imply Peyton personally inspected it. |
| Source-integrity request, appeal, or high-rank review | Resolve exact UUID, SHA, policy, attempt, and current status; then use `backroom-review`. If the run authorizes delegation, dispatch one bounded investigator promptly with those identifiers and the evidence boundary. Follow its result to a supported guarded decision or a specific unresolved check. A court timeout is neither CLEAR nor REJECT. |
| Scoring, validation, or infrastructure complaint | Inspect exact tickets/attempts, provider route, and live health in Backroom. Investigate the cause before retrying. Keep infrastructure failures miner-neutral and never duplicate an active ticket. Route code changes to the owning repository issue/PR. |
| Miner contribution or PR | Verify the PR's author, current head, diff, reviews, checks, and related issue. Review or delegate when authorized; leave precise comments. Approval, workflow execution, and merge each need their own applicable authority and fresh head checks. |
| Moderation | Separate spam/personal attacks from criticism, allegations, and useful evidence. Preserve permalinks and a narrow record. Delete only content covered by the user's moderation authority and the browser's action-time confirmation requirements. Keep legitimate review requests visible. |
| Non-miner DM | Do not send a miner-template reply. If the request asks for a sweep, report a short follow-up table with owner and suggested next step. |

If an identity needed for an exact review is missing, ask for the UUID or full
hotkey while continuing independent investigation. Do not turn that request
into repeated “a task is looking” replies. If a subagent or investigator was
dispatched, say what it is checking; when it finishes, return with the actual
state and action. Apply a supported ruling only within the authority granted
for this run, using the fresh guards and verification in `backroom-review`.

## Communicate without flooding

- Answer DMs with the verified status, what changed, and what the miner can do
  next. Correct earlier attribution promptly when evidence changes.
- Use the public channel for material subnet-wide changes or a requested
  moderation statement. Avoid repetitive bot acknowledgments and public
  allegations based on other miners' claims.
- Distinguish merged code, released code, deployed code, and observed live
  behavior. Distinguish 3/3 canonical scoring from continual shared-seed
  retests and from emission eligibility.
- Report to Peyton only meaningful new issues, completed actions, failures, or
  decisions needing input, with links and exact status. A no-change scheduled
  run stays quiet.

## Hand off the next run

Keep one compact checkpoint: last channel/DM cursor; open investigations with
message link and exact UUID/SHA; assigned investigator or issue/PR; last
verified state; action already taken; and the next condition to check. Remove
settled cases. A schedule should invoke this skill with its interval and
authority, then carry only that checkpoint forward; do not accumulate stale
incident narratives or treat a prior release as proof of current deployment.
