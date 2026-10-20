# Signed moderation audit rollout

Signed moderation events are staged off by default. While
`DITTO_MODERATION_AUDIT_ENABLED` is unset or `false`, existing automated and
operator moderation transitions continue without a new public moderation
event. No unsigned moderation event is written.

To activate, apply the Terraform change that creates the empty
`platform-moderation-audit-signing-key` Secret Manager container and grants the
Platform runtime service account read access. Generate a 32-byte Ed25519 seed
with a secure secret generator, encode it as 64 hexadecimal characters, and
add it as a Secret Manager version outside Terraform. Do not put the seed in
Terraform variables, state, logs, or this repository.

Converge each Platform app host with `platform_moderation_audit_enabled: true`.
The Ansible role reads and validates the secret before rendering the enabled
environment. Confirm every running API host has the same public signer key and
that a test moderation transition produces a verifiable signed event before
relying on the public feed. Day-two code releases reuse the host environment;
they do not rerun this Ansible role.

If the key is missing or malformed after activation, the affected moderation
transaction fails rather than writing an unsigned event. Set the rollout flag
back to `false` on every host to restore the staged behavior while repairing
the key. Keep old public keys in
`DITTO_MODERATION_AUDIT_PREVIOUS_PUBLIC_KEYS` when rotating the signer so
existing records remain verifiable.
