# Depot CI migration

## CI sizing and custom image

The Platform Python suite is CPU-bound. On the default Depot `2x8` sandbox,
pytest-xdist created two workers and spent 329.58 seconds running 4,159 tests.
Dependency installation took less than two seconds, while cold service-image
pulls and startup added about twenty seconds.

`.depot/workflows/platform-ci.yml` therefore uses two Depot-native features:

- a `ditto-platform-ci` snapshot preinstalls Python dependencies and preloads
  the pinned Postgres and MinIO images;
- the test consumer uses an `8x32` sandbox, allowing the existing `-n auto`
  pytest configuration to create eight workers.

The consumer still runs `uv sync --locked`. This is intentionally redundant:
it makes a lockfile change correct immediately even if the three-day snapshot
is reused. `actions/checkout` uses `clean: false` so it does not delete the
snapshot's installed `.venv`.

## Deployment identity

Depot CI tokens use `https://identity.depot.dev`, not GitHub's OIDC issuer.
The existing GitHub WIF provider and its environment-scoped subjects cannot
authenticate a Depot job.

The GCP Platform stack creates a separate `depot-ditto-subnet` provider. Its
attribute condition accepts only all three of:

- Depot organization `4q2czr6whg`;
- repository `ditto-assistant/ditto-subnet`;
- ref `refs/heads/main`.

That main-ref condition is the production branch gate. Depot reads a job's
`environment` field for selecting secret variants, but it does not enforce
GitHub Environment branch restrictions or required reviewers. The new
principal is bound in parallel to every GCP service account used by automatic
release and runtime deployment jobs; the existing GitHub bindings remain in
place for a reversible cutover.

After a reviewed `gcp-platform` plan and apply, store the
`depot_wif_provider` output as `GCP_WIF_PROVIDER` in Depot. Restrict the secret
variant to repository `ditto-assistant/ditto-subnet`, branch `main`, and the
eventual release/deployment workflow paths.

Once that apply is complete, run the GitHub-hosted **Bootstrap Depot deployment
secrets** workflow from `main`. Its `prod` and `dev` jobs deliberately enter the
corresponding protected GitHub environments so GitHub can release the existing
values, then write main-, environment-, repository-, and workflow-scoped Depot
variants. It writes the new Depot provider resource name rather than copying
the incompatible GitHub issuer's provider. Depot does not expose secret values
after import, so verify the result by name and availability scope in Depot and
then perform an OIDC authentication probe before moving a deployment trigger.

## Why `release.yml` is not switched yet

The release workflow has two external gates that code in this pull request
cannot safely pretend are complete:

1. The new GCP provider and IAM bindings must be applied before a Depot job can
   exchange its OIDC token.
2. Several release jobs publish public images to GHCR with `GITHUB_TOKEN`.
   Depot CI uses a GitHub App token, which GHCR does not accept for package
   pushes. Depot Registry requires authenticated pulls, while validators and
   public fleet consumers currently pull these images anonymously.

The safe image choices are either a dedicated GitHub PAT with `write:packages`
stored as a main/release-only Depot secret, or a separately designed migration
of every public image consumer to another public registry. Depot Registry can
still be used as the fast build store and then copy the immutable result to the
public distribution registry.

Keep `.github/workflows/release.yml` active until the WIF apply and registry
credential/distribution decision are complete. Infrastructure plan/apply also
stays on GitHub because `infra-apply` requires a protected-environment reviewer,
which Depot does not enforce.
