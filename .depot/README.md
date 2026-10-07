# Manual Depot validation fallback

GitHub-hosted runners own automatic PR validation. This repository is public,
so its standard GitHub runners are free. The Depot workflow copies remain in
organization `4q2czr6whg` for explicit operator use during an Actions outage or
when faster validation is worth the cost. They have **no automatic PR, push or
scheduled triggers**: entrypoints are `workflow_dispatch` only and reusable
verifiers are `workflow_call` only.

Four-CPU / 16-GB Depot sandboxes retain the same sharding, locked dependencies,
real PostgreSQL services, MinIO integration, immutable actions and test commands.
`ditto/tests/test_depot_ci.py` compares the executable jobs with their GitHub
copies to prevent drift. Update both definitions when shared validation changes.

## Run the fallback

Use a clean checkout of the exact **pushed PR head**. The CLI uploads local
changes as a patch, so a local-patch run is useful for iteration but is not
evidence that the pushed head passed. Resolve the PR head with `gh pr view`,
fetch it, and compare `git rev-parse HEAD` before running:

```sh
git status --porcelain
git rev-parse HEAD
depot ci run --org 4q2czr6whg --workflow .depot/workflows/ci.yml
depot ci run --org 4q2czr6whg --workflow .depot/workflows/platform-ci.yml
```

Choose the workflows applicable to the PR; `.depot/workflows/backroom-ci.yml`
and `model-relay.yml` validate their components. Invoke `ci.yml` and
`platform-ci.yml`, not the reusable verifier files directly: the parent passes
the exact ref and verification mode. The relay's static release-binary check
also runs manually. `conventional-pr.yml` resolves the title of exactly one
open same-repository PR at the run SHA when no PR event payload is present;
it fails if there is no unique matching head.

The only required status on `main` is `migration-order/merge-result`. Its normal
PR owner and authoritative queued main sweep stay on GitHub. The fallback can
run the same per-head migration proof and publish the same status:

```sh
depot ci run --org 4q2czr6whg --workflow .depot/workflows/platform-migration-order.yml
```

Wait for completion, inspect the run source SHA and job conclusions, and verify
`migration-order/merge-result` on the current PR head. A dispatch, local patch,
historical check or a skipped job is not passing evidence. If the head changes,
validate the new head. Do not bypass failed or missing applicable validation.

## Reliability boundary

Depot provides independent validation compute and scheduling during a GitHub
Actions outage. It still needs GitHub repository access and check/status APIs;
it cannot guarantee merges during a broader GitHub outage. A Depot usage cap
or Depot outage can also block fallback runs. Retaining manual workflows avoids
paying for duplicate automatic validation on every push.

Protected releases, deployments and the authoritative migration sweep stay on
GitHub. No cloud identity, production secret or deployment trigger is copied to
Depot. `preview.yml` runs only unprivileged plan and cheatcode validation;
GitHub owns dashboard publication and its same-run artifact boundary. Depot
validation does not replace that publisher during an Actions outage.
