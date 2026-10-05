# Depot validation

Same-repository PR checks for root, Platform, Backroom, Model Relay and the
conventional title run on Depot CI in organization `4q2czr6whg`, following the
backend repository's validation-only setup and subnet PR #243. These are current
workflow copies, not the older test definitions from that PR. Four-CPU / 16-GB
Linux sandboxes retain the existing sharding, locked dependencies, real
PostgreSQL services, MinIO integration, immutable actions and test commands.

GitHub retains fork PR validation and manual rollback. Its reusable verifiers
remain available to the protected release gate. The authoritative migration
status and protected release/deployment workflows stay on GitHub. No cloud
identity, production secret or deployment trigger is copied to Depot.

The Depot preview workflow runs only plan and cheatcode validation. GitHub's
existing preview workflow still owns its protected publisher and same-run
artifact boundary; it is intentionally not replaced with a Depot publisher.

Run a validation against the current checkout (local changes are uploaded):

```sh
depot ci run --org 4q2czr6whg --workflow .depot/workflows/platform-ci.yml
```

The installed Depot Code Access app reports automatic PR jobs as GitHub checks;
verify their exact pushed head. The CLI is also available for pre-push iteration:
a passing local-patch run is separate from the pushed-head PR check. Do not merge
while an applicable Depot validation fails or has not run.

`test_depot_ci.py` compares the executable jobs with the GitHub verifier copies
to prevent command, service or security drift. Update both definitions whenever
the shared validation changes. To roll back validation, remove the Depot-only
PR conditions from the five GitHub entrypoints and disable the corresponding
Depot triggers together; releases continue to use GitHub throughout.
