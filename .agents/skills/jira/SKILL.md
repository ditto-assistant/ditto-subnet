---
name: jira
description: "Keep Ditto work in sync with Jira (DITTO, SN, GTM) via the Atlassian MCP: find or create the work item, keep its status current, and put its key in PR titles. Use when starting work, opening a PR, after merge, filing a bug, or asking what to work on."
---

# Jira (Ditto)

**User Input**: $ARGUMENTS

Jira is where Ditto work is prioritized. GitHub holds the code and technical discussion. Every PR you open should name the Jira work item it delivers, and that work item's status should match reality when you finish.

## Constants

- **Site / cloudId**: `https://omniaura-team.atlassian.net` (UUID `492f4031-0349-4bcf-a05b-d0913759db8e`). Pass it directly. Don't call `getAccessibleAtlassianResources`.
- **MCP server**: Atlassian Rovo MCP at `https://mcp.atlassian.com/v2/mcp`. Tool names look like `mcp__<server>__createJiraIssue`, and the server may be named `atlassian` or `jira` locally.

| Project | Key | Repos | Types |
|---|---|---|---|
| Ditto | `DITTO` | backend, ditto-app, console, backroom, ditto-cli, ditto-desktop, ditto-mcp, ditto-review, infra, heyditto-stack | Epic, Feature, Bug, Task, Incident, Sub-task |
| Ditto Subnet (SN118) | `SN` | ditto-subnet, ditto-subnet-stack, turbobt | same |
| Go-To-Market | `GTM` | landing-astro, brand-kit, content-house, video-ad-studio | Task, Sub-task |

**DITTO / SN workflow**: `Triage → Ready → In Progress → In Review → Verify → Done`. Any transition is allowed. Use the Jira flag for blocked items.

| Status | Means |
|---|---|
| Triage | New and not yet prioritized. Everything you create starts here unless a human said it's prioritized. |
| Ready | Prioritized and ready to pull. |
| In Progress | Someone (or their agent) is building it. |
| In Review | PR(s) open. |
| Verify | Merged. `main` deploys on merge, so this means "confirm it works in prod". |
| Done | Confirmed working in production. |

**DITTO components**: Inference/Router · Chat & Agents · Workflows & Automations · Memory & Retrieval · MCP & Integrations · Ditto Code · Orgs/Billing/Onboarding · Platform (CI/Preview/Deploy) · Clients (desktop/mobile/CLI) · Backroom/Console · Security.
**SN components**: backroom, bench, dashboard, infra, platform, screener, treasury, validator.
**Source field** (DITTO): `customfield_10077`, one of `feedback-report`, `customer`, `internal`, `incident`, `board`. Set it as `{"customfield_10077":{"value":"internal"}}`.

People and account IDs: see [people.md](people.md). Map GitHub handle → Jira account through that file. Never guess an account ID.

## What a work item is

A work item is an outcome (a feature, bug fix or task) that one or more PRs deliver, possibly across repos. The team merges ~2,300 PRs a month, so:

- Don't create a work item per PR. A backend PR and its ditto-app PR share one key. So does a stack of five PRs.
- Phrase summaries as outcomes: "Workflow schedule honours weekly recurrence", not "fix(workflows): cadence guard".
- Group a work item under an existing epic when one fits. Don't create epics unless asked.

## Workflows

### Start work (`work on …`, a task description, or a new branch)

1. **Find the item.** If the user gave a key, `getJiraIssue` it. Otherwise search with JQL before creating anything:
   `project = DITTO AND statusCategory != Done AND text ~ "<2-4 distinctive words>" ORDER BY updated DESC`
2. **If nothing matches, create one** in the right project: type, outcome summary, components, `Source`, priority (default Medium), and parent epic when obvious. Tell the user the new key.
3. **Transition to In Progress** and assign it to the person you're working for (the git author or the user) if it's unassigned. Use `getTransitionsForJiraIssue` to find the transition ID; don't hard-code it.
4. Carry the key through the session.

### Open or update a PR

- **Put the key at the end of the PR title**: `feat(workflows): honour weekly cadence [DITTO-123]`. Conventional-commit titles stay valid, and GitHub for Jira links the PR from the title.
  - Branch names are optional; agent branches like `codex/…` are fine.
  - For several items, list each key: `[DITTO-12][DITTO-40]`.
- **Put the key in the PR body too**: `Jira: DITTO-123` near the top. For cross-repo work, link the sibling PR.
- **Status**: once a PR is open, transition the item to In Review, unless Jira automation already did (read the status first).
- **Comment on the item** with the PR URL(s) only when the GitHub link isn't showing on it.

### After merge

- **If all PRs for the item are merged**, transition it to Verify. Comment with what to check in production and how: the endpoint or screen, the log query, the metric.
- **If more PRs remain**, leave the status alone and comment what's left.

### Verify → Done

Move to Done only with evidence: a prod check you ran, a user confirmation, or a metric. Say which one in a comment. If the item's `resolution` is still empty after the transition, set it with `editJiraIssue` `{"resolution":{"name":"Done"}}`. Otherwise JQL `resolution = Unresolved` keeps counting the item as open. If you can't verify, leave it in Verify and tell the user what needs checking.

### File a bug, incident or follow-up

1. **Search first** to avoid duplicates. If you find one, comment on it instead of creating a new item.
2. **Create in Triage.** Use type `Bug` or `Incident`, and set `Source` (`feedback-report` for app feedback, `incident` for prod events).
3. **Write the description:** what happened, the impact, repro or evidence (error `ref`, request ID, log query), and suspected area with file paths.
4. **Outages or security events** get priority Highest. Tell the user so a human can alert the team.

Follow-ups discovered while working (TODOs you leave, review comments you defer) become Tasks linked with `Relates` to the current item. Don't leave them only in PR text.

### What should I work on? / board status

- **Ready queue**: `project = DITTO AND status = Ready ORDER BY priority DESC, rank ASC`
- **My work**: `assignee = currentUser() AND statusCategory != Done ORDER BY status`
- **Review wait**: `project = DITTO AND status = "In Review" AND status changed to "In Review" before -24h`
- **Needs verification**: `project = DITTO AND status = Verify ORDER BY updated ASC`

Summarize in chat. Don't paste raw JSON.

## Rules

- **Never put secrets, tokens, user emails, customer PII or prod data in Jira.** ditto-app will be open-sourced. GitHub is public-facing, but Jira isn't a vault either.
- **Make MCP calls one at a time.** The Atlassian server rate-limits bursts (about 20 parallel calls get a 429). On a 429, wait and retry.
- **Don't delete work items, change workflows or reassign other people's in-progress work.** Leave a comment and tell the user instead.
- **If the Jira MCP isn't connected** (no `*JiraIssue` tools), keep working. Still put the user-provided key in the PR title, and tell the user to run `/mcp` to authenticate the Atlassian server. Never invent a key.
- **Don't create Jira items for dependabot, chores or `DO NOT MERGE` eval PRs.**
