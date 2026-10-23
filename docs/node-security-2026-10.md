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

At October 22, 2026 21:26:58 EDT, a read-only inventory authenticated to
`subnet-screener-1` over its company-tailnet address as the documented `peyton`
user. SSH verified the existing trusted key under the inventory's public-IP
alias with strict host-key checking; no trust entry was added or bypassed.
No Node binary was found on the login PATH or at `/usr/bin/node`,
`/usr/local/bin/node`, or `/opt/nodejs/*/bin/node`; no process named `node`
or `nodejs` and no running root-Docker container were observed. The retained
`screener-fleet-stable-1` image was
`sha256:7d131cbe673fded89878aac86e364ca4e1d4c8d9b543096e6d5bc3cd049faf59`,
source `27cd188027e9dd674af4cb85417f9220dc0ce0a5`. This bounded check does not
exhaust user installations, rootless container engines, inactive images,
other hosts, or previously shipped benchmark images. The Platform app host
and its pm2 daemon remain unverified. No production mutation was performed.

After authorized merge/release, rebuild and publish the affected images and
verify the deployed image digest plus `node --version` in the actual runtime.
Platform provisioning now reads any remaining `/usr/bin/node` and the actual
`/proc/<pid>/exe` of each existing Platform-owner PM2 daemon before host writes.
It refuses to proceed if any observed version is affected or unsupported,
including an old daemon behind an already-updated `/usr/local/bin/node` link.
Fresh hosts and patched supported versions pass this admission check. It never
purges distro packages or restarts the application. Existing-host upgrades must
first inventory package dependents and complete separately authorized Node
retirement and/or PM2 maintenance, then rerun the role. A separately authorized
converge selects the fixed host binary only after those checks pass.
Do not treat a new symlink or a successful CI build as running-process proof.
