# Monorepo releases and hosted deploys

`release/components.toml` is the source of truth for release coupling. A path
selects its own component and reverse-dependents only. In particular, a miner
CLI-only change does not build the validator stack, while a DittoBench API
change rebuilds the digest-bound stack directly from the same checkout. There
is no cross-repository version-bump pull request.

The `Release` workflow first verifies the exact merged source and then creates
one semantic monorepo release. Hosted deploys and image publication consume the
resulting immutable release commit:

- Platform uses the reusable exact-SHA IAP deploy and migrates from
  `/opt/ditto-subnet/apps/platform`;
- Backroom builds and deploys `backroom.dittobench.ai` from the same release;
- hosted DittoBench builds its Cloud Run runtime from the release commit;
- screener image publication queues a dedicated Targon Kaniko rental first,
  then uses the existing GitHub/GCP build runner only after an explicit
  provider fallback;
- the five-minute GCE screener reconciling deploy resolves the latest GitHub
  release tag instead of deploying an arbitrary current `main` SHA.

Manual Platform and screener dispatches also require the selected commit to be
the target of a semantic `vX.Y.Z` release tag. The visible `force` input is the
break-glass path for an exact non-release commit; using it delegates the
exception to the protected environment reviewer and is never automatic.

## Protected environment configuration

The `prod` GitHub environment must allow only `main` and contain:

- `GCP_WIF_PROVIDER`
- `GCP_PLATFORM_DEPLOY_SA`
- `GCP_SUBNET_BUILD_SA`
- `GCP_DITTOBENCH_DEPLOY_SA`
- `GCP_SCREENER_DEPLOY_SA`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- the existing Platform deploy-time application values

The `dev` environment needs only the Platform deploy identity and application
values. Infra outputs the service-account emails and exact WIF provider. No
static GCP key is used.

The release job reads `screener-controller-api-token-prod` through its dedicated
WIF identity into a mode-0600 runner file. The Targon key is never a GitHub
secret: only the private capacity VM may read `TARGON_API_KEY` from Secret
Manager, and the build rental receives a 30-minute registry-only token.

## First activation order

1. Merge and apply the infra stack, but leave the capacity-controller flag off.
2. Configure the protected GitHub environments and populate the controller
   bearer secret version out of band.
3. Publish the pinned maintained Kaniko executor.
4. Deploy Platform from a reviewed release so the trusted-build queue migration
   and controller API exist.
5. Enable and converge the capacity controller and separate image-builder unit.
6. Queue one screener build. Verify either a Targon immutable digest or an
   audited GCP fallback in Backroom.
7. Exercise GCE worker scale `0 -> 1 -> 0` before retiring the pet screener.

None of these activation steps is performed by merging infra. Terraform apply,
Ansible convergence, secret population, and production deployment remain
explicit operator actions.
