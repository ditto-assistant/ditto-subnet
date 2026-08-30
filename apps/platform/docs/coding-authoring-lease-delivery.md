# Shadow coding authoring-lease delivery

Platform exposes one authenticated authoring-only route:

```text
POST /api/v1/validator/coding-shadow/authoring-lease
```

The request signs a domain-separated message over validator hotkey, ticket
UUID, one-use nonce, and UTC request timestamp. Platform verifies the signature,
freshness, current chain permit, nonce, and ticket ownership before reading the
private catalog. The nonce row is retained until that signed timestamp leaves
the five-minute freshness window, so a future-dated request cannot be replayed
after janitorial deletion while it would still be accepted. A wrong-validator
or expired ticket therefore cannot trigger a private object read.

Platform then reconstructs the complete task lease, rechecks the current exact
artifact certification, and invokes an authoring-only minter. That minter
neither checks nor signs the grader object. The wire response contains exactly
these ordered capabilities:

```text
visible-bundle
memory-bundle
resource-profile
```

The response binds the shared run manifest and digest, task-set digest,
repository epoch, issue/runtime/budget material and digests, ticket deadline,
and `weight_eligible=false`. It is `Cache-Control: no-store`. Grader capability,
grader tests, gold patches, catalog coordinates, source URLs, policy labels,
and curator metadata are structurally absent.

The root validator client has a typed request method with a 512 KiB response
ceiling and redacted parse errors. No worker or scheduler calls the method.

## Activation boundary

This route can serve only an already-issued explicit shadow ticket. No
production scheduler issues work to it, no validator worker invokes it, and it
does not start a workspace, issue a Luna grant, grade a patch, write a score,
deploy a service, or affect emissions. Coding contract v1 remains permanently
weight-ineligible.
