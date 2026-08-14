# Monorepo releases and hosted deploys

`release/components.toml` is the source of truth for release coupling. A path
selects its own component and reverse-dependents only. In particular, a miner
CLI-only change does not build the validator stack, while a DittoBench API
change rebuilds the digest-bound stack directly from the same checkout. There
is no cross-repository version-bump pull request.

The scorer's Go module replaces `dittobench-datagen` with the in-tree
`research/dittobench-datagen` module. A datagen change therefore selects the
generator, scorer, and validator-stack components in one release plan.

The `Release` workflow first rejects a merge that a newer queued `main` push
already superseded. For the current merge, the root and every selected
component verify the exact source in parallel before one aggregate gate permits
the semantic monorepo release. Hosted deploys and image publication consume the
resulting immutable release commit:

- the miner starter kit is released from `miners/dittobench-starter-kit`
  without rebuilding the validator stack;
- Platform uses the reusable exact-SHA IAP deploy and migrates from
  `/opt/ditto-subnet/apps/platform`;
- Platform API/runtime changes also select Backroom, so both build and deploy
  from the same release. Dashboard-only changes deploy Platform without the
  practically unrelated Backroom redeploy;
- Backroom deploys `backroom.dittobench.ai` automatically and preserves its
  Cloudflare-encrypted Worker secrets;
- hosted DittoBench builds its Cloud Run runtime from the release commit;
- datagen publishes an immutable component digest, stages it on a zero-traffic
  Cloud Run revision, verifies an authenticated v8 generation, and only then
  promotes it to 100% traffic;
- screener image publication queues a dedicated Targon Kaniko rental first,
  then uses the existing GitHub/GCP build runner only after an explicit
  provider fallback;
- an assembled, signed validator-stack descriptor advances the non-activating
  `candidate-compat-2` channel while remaining smoke tests continue, allowing
  opted-in validators to authenticate and pre-pull its exact component images;
  only the later `compat-2` promotion authorizes a transactional update;
- the capacity controller and its trusted-builder sibling deploy together from
  the exact release commit over IAP whenever either orchestrator or screener
  source changes;
- the five-minute GCE screener reconciling deploy resolves the latest GitHub
  release tag instead of deploying an arbitrary current `main` SHA.

Release planning starts from the latest published semantic tag so queued or
failed pre-tag runs carry their changes into the next attempt. Once semantic
release has published a tag, that tag becomes the next planning baseline even
if a downstream deploy fails. Recover that release by re-running its failed
jobs; do not rely on a later source push to select already-tagged components.
Before spending runners on exact-source verification, a release attempt
refreshes `origin/main` and reports an already-stale merge as superseded. The
release job repeats that check immediately before making any version, tag, or
release mutation. If the verified merge is no longer current, it exits
successfully without releasing or deploying; GitHub's latest queued `main` run
then carries every change since the last published tag. Python Semantic Release
retains its own upstream check as the final fail-closed guard for a push in the
remaining race window.

## Release runner capacity

Root exact-source verification keeps formatting, lint, typing, and integration
checks in one lane while three standard-runner matrix shards split the ordinary
pytest node IDs deterministically in collection order. A stable fan-in job
fails closed unless the static lane and every shard succeed. The v0.63.6
release spent 125 seconds running 1,553 ordinary tests serially, so the split
removes roughly one minute from the pre-release critical path without using
billed runners or running any test concurrently inside one checkout.

Platform pull-request CI and exact-source release verification both call
`.github/workflows/platform-verify.yml`. The reusable workflow runs static and
dashboard gates independently and splits the complete backend suite across four
standard public-repository runners. Release planning selects the backend and
dashboard surfaces separately, so neither surface reruns for an unrelated
Platform component.

The later DittoBench scorer build publishes one native child on each of the
organization-configured GitHub-hosted `ubuntu-24.04-release-8core` and
`ubuntu-24.04-release-arm64-8core` runners. Both are in the
`release-larger-runners` group, which is limited to this repository and
`release.yml` on `main`; each architecture is capped at one concurrent runner.
The fan-in job combines those immutable digests into the canonical
multi-platform index. This avoids running the cgo/tree-sitter arm64 compile
through QEMU: it took 2m38s under emulation in v0.62.2 versus 19.4s in a
cacheless native arm64 validation.

The scorer's checksum-pinned LongMemEval input downloads in a deterministic
Docker `RUN` layer. A clean builder still fetches the immutable upstream URL
and fails unless its full SHA-256 matches, while subsequent release builds
restore the already-verified bytes from each architecture's BuildKit cache.
Do not switch this back to remote `ADD`: BuildKit revalidates that URL on every
build, which put 110 seconds of avoidable network work on the v0.63.4 arm64
critical path.

The validator image follows the same immutable native-child pattern, but uses
the standard `ubuntu-24.04` and `ubuntu-24.04-arm` runners. Standard-runner use
is free for this public repository, and keeping the validator off the bounded
larger-runner group lets its two builds run concurrently with the scorer builds.
The v0.63.2 release's emulated multi-platform validator job took 3m53s,
including 3m24s in the build-and-publish step.

Stack assembly keeps authentication fail-closed while overlapping independent
network work. The four first-party image indexes authenticate concurrently;
Pylon authenticates in its own build lane before that lane can satisfy the
assembly dependency; and the generated runtime smoke pulls its two exact
dependency images concurrently before startup. In v0.63.2 those serialized
operations occupied about 92 seconds of the assembly critical path.

Do not move short planning, tagging, verification, deployment, smoke, or
promotion jobs to the billed pools without measured job-level evidence. If
either larger runner or the group is removed, restore its native build job to
the matching standard GitHub-hosted architecture in the same change so
releases cannot queue indefinitely on a missing label.

Semantic release writes the monorepo version into datagen's Go provenance,
publishes the source-SHA tag once, and attaches that same monorepo release tag
to the exact digest so a partial rerun converges without overwriting immutable
tags. Semantic release, not Terraform, owns the running datagen image version.
Terraform owns its service shape and least-privilege release IAM, and ignores
image drift after the one-time creation bootstrap so an unrelated apply cannot
roll the generator back.

Manual Platform and screener dispatches also require the selected commit to be
the target of a semantic `vX.Y.Z` release tag. The visible `force` input is the
break-glass path for an exact non-release commit; using it delegates the
exception to the protected environment reviewer and is never automatic.
The capacity-controller workflow's direct manual dispatch is a separate
break-glass operator override: it intentionally bypasses
`SCREENER_CAPACITY_CONTROLLER_ENABLED`, while still requiring the protected
`prod` environment and an exact 40-character revision.

## Protected environment configuration

The `prod` GitHub environment must allow only `main` and contain:

- `GCP_WIF_PROVIDER`
- `GCP_PLATFORM_DEPLOY_SA`
- `GCP_SUBNET_BUILD_SA`
- `GCP_DITTOBENCH_DEPLOY_SA`
- `GCP_DATAGEN_RELEASE_SA`
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

Backroom application secrets are not Terraform values. Bootstrap them once
with `apps/backroom/scripts/bootstrap-worker-secrets.sh`; the script consumes
Google's OAuth JSON locally, streams `ADMIN_API_PASSWORD` from GCP Secret
Manager, and installs the encrypted Worker bindings in one Cloudflare deploy.
Only the scoped Cloudflare deployment token belongs in the GitHub environment.

## First activation order

1. Merge and apply the infra stack, but leave the capacity-controller flag off.
2. Configure the protected GitHub environments and populate the controller
   bearer secret version out of band.
3. Publish the pinned maintained Kaniko executor.
4. Deploy Platform from a reviewed release so the trusted-build queue migration
   and controller API exist.
5. Enable and converge the capacity controller and separate image-builder unit.
   Later semantic releases deploy both units automatically; Ansible remains the
   first-boot/configuration path.
6. Queue one screener build. Verify either a Targon immutable digest or an
   audited GCP fallback in Backroom.
7. Exercise GCE worker scale `0 -> 1 -> 0` before retiring the pet screener.

Merging application source performs semantic release and automatic runtime
deployment. Infrastructure remains separate: Terraform apply, first-boot
Ansible convergence, and initial secret population are explicit operator
actions backed by a reviewed plan.
