# Ditto people → Jira accounts

Use these IDs for `assignee_account_id` / `{"assignee":{"accountId":"…"}}`. Match by GitHub handle (PR author, `git log` author) first, then by display name. If someone is not listed, leave the item unassigned and write "Suggested owner: <name>" in the description.

| Person | GitHub | Jira accountId | Usual areas |
|---|---|---|---|
| Peyton Spencer | `Peyton-Spencer` | `712020:a9cf187d-73b6-4197-a6c3-118b9a57f55b` | Most areas; infra, previews, router, SN |
| Omar Barazanji | `omarzanji` | `712020:18fd1e6f-13d7-4cb8-abf6-5dc1ace6e2de` | V2 app, Ditto Code, product |
| Nick Anderson | `Tetramputechture` | `712020:55aab230-f499-4e5e-8047-c914df9afdcf` | Workflows, memory/retrieval, encryption |
| Alan Gothard | `AlanGOmniAura` | `712020:68145f86-5e8b-47c2-a703-c21606cfb1b7` | Frontend, V2 UI, design |
| Nick Allen | `nicktallen` | `712020:b1faed5c-86b0-487d-9bdd-85a481ac5a90` | Frontend / mobile |
| Romanee Spencer | — | `712020:64d1d4e0-2217-481c-b5bf-322845be6605` | GTM: demo tracker, coordination |
| Alex Patrick | — | `712020:6514728f-579f-4e06-ab72-7bf839a30cd9` | GTM: branding |

No Jira account (leave unassigned, name them in the description): Louise Beattie (content/social), Taylor Norman (Praxis, user testing), Jared Lawrence (Beyond Finance, board), Alex Chapman (SEO Partners; not Alex Patrick), external subnet contributors.

Last verified 2026-09-27. If a lookup is needed, use `lookupJiraAccountId` with the person's work email.
