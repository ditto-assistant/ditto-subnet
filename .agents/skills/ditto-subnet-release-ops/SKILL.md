---
name: ditto-subnet-release-ops
description: Design, implement, audit, or operate ditto-subnet semantic releases, affected-component CI, container builds, automatic application deployments, validator rollouts, screener autoscaling and trusted builds, Targon-first capacity with GCE fallback, GCP IAM/WIF, Cloudflare Workers, Terraform, Ansible, and rollback. Use for release or live-runtime work where exact SHA, credentials, provider safety, or activation boundaries matter.
---

# Ditto Subnet Release Ops

Preserve one exact monorepo release identity while keeping application deployment and infrastructure authority separate.

## Orient

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py \
  --max-topics 4 "$ARGUMENTS"
```

Pass the user's task text verbatim. If it is empty, omit the query and begin
from the monorepo overview rather than injecting every release owner.

Read [`references/release-ops-index.md`](references/release-ops-index.md), then inspect the exact workflow, Terraform stack, or live runtime involved.

## Work from evidence

1. Resolve the requested PR/commit/release and current `main` independently.
2. Inspect the affected-component plan before predicting builds or deploys.
3. Validate workflow permissions, immutable action refs, environment protection, WIF subject, and rollback path.
4. For a live claim, verify deployed SHA/image digest, service health, and client-visible behavior.
5. State what is implemented, merged, released, deployed, applied, and activated separately.

## Authority and secrets

- Automatic application deploys may follow a semantic release from `main`.
- Terraform always uses reviewed plan and protected apply; application workflows do not apply infrastructure.
- Never read or print `TARGON_API_KEY` or other provider secrets. Use Secret Manager indirection and tests that consume values without returning them.
- Never place cloud, GitHub, Platform, or provider credentials in untrusted build/runtime environments.
- Do not create service accounts or IAM bindings out of band merely to bypass an unapplied Terraform bootstrap.

## Capacity invariants

Targon is primary. GCE normally targets zero and is a bounded residual/failure fallback. The controller must be fenced and count pending workers; an independently fenced GCP watchdog may add fallback capacity only when backlog exists and the primary heartbeat is stale. Fail closed when provider isolation cannot be proven.
