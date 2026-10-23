# October 2026 Node security update (GTM-89)

The [October 21 Node advisory](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases)
fixes the supported lines at 22.23.4, 24.22.1 and 26.12.1. This change raises
our explicit CI, container, developer and package floors to those releases.
It does not merge, release, redeploy, converge Ansible, or restart a host.

## Source inventory at 6d55aa7b7d024b0e1a6ac773e68bff817bd4015b

| Surface | Before | After |
| --- | --- | --- |
| Coding Node production and confinement images | 24.20.0, digest pinned | 24.22.1, digest pinned |
| OpenClaw adapter image | 24.15.0 | 24.22.1, digest pinned |
| Backroom preview container | 22.22.0 | 22.23.4, digest pinned |
| Dashboard preview build container | 22.22.2 | 22.23.4, digest pinned |
| Coding CI | 24.20.0 | 24.22.1 |
| Other explicit GitHub/Depot Node setup | floating 22 or 24 | 22.23.4 or 24.22.1 |
| Platform host provisioning | unversioned distro nodejs/npm | official 24.22.1, SHA-256 pinned for x64/arm64 |
| Owned JavaScript manifests | absent/broad minimum, including Node 25 | ^22.23.4 or ^24.22.1 or ^26.12.1, engine-strict |

Docker Hub manifests for each replacement tag and Node's official
[24.22.1 checksums](https://nodejs.org/dist/v24.22.1/SHASUMS256.txt) were
retrieved on October 22, 2026. These are source/artifact findings; they do not
establish which old image or binary is currently running.

## Exposure assessment

The pinned pre-fix container versions are affected runtime versions according
to the advisory. The coding driver imports submitted candidate code into a
separately confined non-root process. Its native Linux seccomp layer is defense
in depth, not evidence that a Node memory-corruption vulnerability is unreachable
or a host-escape proof. No exploit probes were performed.

The OpenClaw adapter uses Node HTTP and fetch for validator/model endpoints;
its inbound JSON reader does not itself decode Brotli. Transitive OpenClaw
network/decompression behavior was not exhaustively audited. The absence of a
Brotli call in our small adapter is therefore not a finding of no exposure.
TLS/DNS advisory conditions require analysis of the deployed client paths,
hostname inputs, upstream responses and network boundaries; source pins alone
do not establish a successful attack. No evidence of exploitation was collected.

Backroom production is a Cloudflare Worker, and the public dashboard is static
browser assets. A Node build version does not establish the Node version in
Cloudflare's hosted workerd runtime or a Node server behind those assets.
Platform's API is Python, but its host provisioning also installs Node for pm2
and dashboard builds. Its running pm2 daemon needs a separate version audit.

## Remaining rollout evidence

The read-only screener-host inventory attempt using the documented `peyton`
identity failed SSH host-key verification. No host-key bypass or production
mutation was attempted. Live installed/process versions, container digests,
old distro binaries and any previously shipped benchmark images remain unverified.

After authorized merge/release, rebuild and publish the affected images and
verify the deployed image digest plus `node --version` in the actual runtime.
A separately authorized Platform Ansible converge selects the fixed host
binary; a running pm2 daemon can retain its previous executable until an
operator restarts/updates it. Inventory and plan that maintenance separately.
Do not treat a new symlink or a successful CI build as running-process proof.
