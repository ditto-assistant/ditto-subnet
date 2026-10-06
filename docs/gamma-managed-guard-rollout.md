# Managed Gamma guard rollout

The signed stack exposes an opt-in public policy proof to Pylon and the
validator only. Ordinary stacks remain unarmed. This does not enable the
Platform producer or any collector signer timer.

Stage only the `emission_approval` JSON object at
`/var/lib/ditto-validator/treasury/emission-approval.json`, root-owned, bounded
at 8192 bytes. Never mount the returned custody packet, private keys, or seeds.
Pin the same exact policy and collector-policy SHA-256 digests in both trusted
processes. Both independently verify the offline signature.

Host settings (public values) are:

```text
DITTO_TREASURY_WEIGHT_ENFORCEMENT=true
DITTO_TREASURY_APPROVAL_HOST_PATH=/var/lib/ditto-validator/treasury/emission-approval.json
DITTO_TREASURY_SHADOW_APPROVAL_FILE=/run/secrets/treasury-emission-approval.json
DITTO_TREASURY_APPROVED_POLICY_DIGEST=<exact approved policy digest>
DITTO_TREASURY_COLLECTOR_POLICY_DIGEST=<exact collector policy digest>
```

The Ansible `validator_stack` role accepts the corresponding explicitly enabled
profile and verifies the fixed, pre-positioned proof's metadata before rendering
its environment. It never provisions signing material or copies proof contents.
For an existing managed stack, stage settings before the normal signed stack
update so that the updater drains work and recreates both trusted processes from
the authenticated descriptor. Do not modify the signed Compose bundle or bypass
its mutation fence with an overlay. Changing `.env` alone does not update running
containers. If the same descriptor is already current, the normal updater is a
no-op; a separately reviewed supervised reconfiguration is then required.

Arming Pylon refuses legacy weight dispatch, including previously queued jobs.
Inspect/drain the selected managed members' queued epoch work before activation.
Require fresh signed heartbeats reporting both version-2 Pylon guards and both
exact digests for every explicitly selected member. An unreadable or invalid
proof cannot advertise readiness. Independent validators are outside that
operator-selected roster; a selected missing, stale, or unpermitted member still
refuses activation.

Only after those guards are ready should public Backroom record the audited
observe/enforce runtime revision, with current CAS and exact policy confirmation.
Verify the next authorized epoch, finalized collector weights and incoming alpha,
then the separately approved bounded canary. Recurring transfers stay off until
the finalized canary is verified.
