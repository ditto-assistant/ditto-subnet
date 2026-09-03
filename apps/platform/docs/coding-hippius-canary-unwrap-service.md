# Isolated Hippius canary unwrap service

## Status

This layer implements only the phase-6 synthetic canary's private-input unwrap
backend. Installation and socket activation are independently default-off. It
adds no authoring or grading backend, public route, scheduler, ordinary Coding
worker, score, weight, or emission path.

The service holds no Hippius credential and has no network address family. It
runs as a dedicated non-root user and group, distinct from Platform and every
execution backend. Platform retains only the client proxy and receives one
32-byte data key after the complete request authority validates.

## Exact two-request authority

The service does not accept arbitrary RSA ciphertext. One mode-`0400`,
single-link, service-owned canonical authority file fixes:

- exact merged source SHA;
- synthetic catalog commitment and selected index;
- transport-manifest and publication-receipt digests;
- ticket, run row, validator, assignment, run-manifest, and deadline authority;
- wrapping-public-key digest; and
- exactly two requests, ordered `authoring` then `grading`.

Both allowed requests must bind the same AAD, ciphertext, and wrapped-data-key
digests. They differ only where the ticket-bound request digest commits the
delivery phase. Before decrypting, the service reconstructs the canonical
`dittobench-coding-hippius-private-input-unwrap-v1` projection and recomputes
its SHA-256; trusting a caller-supplied request digest is insufficient.

`prepare_hippius_canary_unwrap_authority.py` derives this allowlist without a
private key. It loads the protected canary plan, encrypted manifest, and signed
publication receipt, verifies their registration identities, derives both
request digests from the one selected wrapped key, and exclusively writes a
mode-`0600` authority. An operator then installs it as the unwrap service user
with mode `0400` and one link.

```bash
cd apps/platform
uv run python scripts/prepare_hippius_canary_unwrap_authority.py \
  --plan /protected/canary/plan.json \
  --manifest /protected/canary/private-input-manifest.json \
  --publication-receipt /protected/canary/private-input-publication.json \
  --output /protected/canary/new-unwrap-authority.json \
  --confirm "PREPARE HIPPIUS CANARY UNWRAP AUTHORITY"
```

The output contains no wrapped-key bytes, plaintext data key, private key,
credential, endpoint, bucket, object key, URL, task text, or grader content.

## RSA operation

At boot, the service uses the fixed root-owned `/usr/bin/openssl` binary to
derive the private key's public SubjectPublicKeyInfo DER digest. Startup fails
unless that digest equals the authority's wrapping-key digest.

Each allowed request invokes `openssl pkeyutl` with:

```text
rsa_padding_mode:oaep
rsa_oaep_md:sha256
rsa_mgf1_md:sha256
rsa_oaep_label:<aad-sha256-hex>
```

The private key path is service-owned mode `0400` and is never read by Platform
or carried in stdin, stdout, or an environment value. The fixed OpenSSL command
receives that protected path through `-inkey` and only the already allowlisted
wrapped bytes on stdin. Exactly 32 plaintext bytes are accepted. Exact replays
return the same canonical response; the in-memory response set is bounded to
the two approved request identities.

## Socket and process isolation

Systemd owns the Unix listener. The socket unit creates a mode-`0660` socket
owned by the unwrap user and Platform group, while the service runs with its
own non-Platform primary group. This lets the proxy connect without granting
the unwrap service access to the group-readable operator environment.

The service requires systemd socket activation in production, verifies the
received listening file descriptor and exact socket path, and validates the
connected proxy UID/GID with Linux `SO_PEERCRED`. It accepts one bounded
length-framed canonical request at a time. Invalid peers and malformed,
oversized, expired, drifted, or non-allowlisted requests receive no data.

The systemd service enables `PrivateNetwork`, restricts address families to
`AF_UNIX`, removes capabilities and privilege escalation, protects the host
filesystem/kernel/home/device surfaces, and discards stdout. Errors are generic
and contain no key or request material.

## Ansible gates

Two independent defaults remain false:

```yaml
platform_coding_hippius_canary_unwrap_installed: false
platform_coding_hippius_canary_unwrap_service_enabled: false
```

Installation creates the dedicated system user/group, root-owned executable,
protected configuration directory, environment file, and dormant systemd
units. It does not create the private key or authority file. This permits an
operator to install those two files under the final service identity before a
separate converge enables the socket.

Enablement refuses missing, linked, incorrectly owned, or non-mode-`0400` key
and authority files. The broader helper gate additionally requires this unwrap
socket to be enabled and independently verifies its inode and peer identity.

## Remaining activation boundary

No private key, authority file, host-variable enablement, Ansible converge,
service start, socket, provider operation, or live unwrap was performed by this
PR. Authoring and pristine-grading backends remain absent, so phase 6 still
cannot run end to end. A live receipt and phase 7 remain blocked.
