# Coding private object leases v2

Status: proposed shadow-only delivery contract. This document does not retrieve
or decrypt a live private object.

Platform resolves each selected arm to exact encrypted private-input objects.
It verifies the registered release, audit, selection, transport-manifest, byte
count, ciphertext digest, plaintext digest, ticket, phase, and expiry before
delivery.

```text
authoring phase -> visible workspace, issue, policy, selected raw memory,
                   and validator-only resource profile
freeze boundary -> revoke authoring capability
grading phase -> visible workspace, hidden grader, and protected resource plan
```

Memory is authoring-only. Grader material is freeze-gated and never present
during authoring. Grading re-fetches the visible base for the pristine
networkless grader.

The miner, executor, model process, and validator never receive a Hippius
credential, bucket name, wrapping key, arbitrary object key, list capability,
or reusable upload URL. Platform and its trusted helper retrieve one registered
object at a time and expose only the phase-appropriate projection.

Any digest mismatch, replay, expired authority, decryption failure, missing
object, or ambiguous read is trusted infrastructure and fails closed. It never
creates a replacement task, fresh selection, or candidate-caused score.
