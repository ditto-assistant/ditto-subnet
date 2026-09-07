# Native-v2 private key custody service

This explicit private service completes the native unwrap protocol used by
`ProcessPrivateV2Unwrapper`. It does not start from the Platform API, install a
system service, provision a key, register a release or enable a Coding gate.
The public control signer in PR #1696 is a separate key and responsibility;
this custody layer does not depend on that PR's unmerged implementation.

## Authority before key access

`PrivateV2InputAuthority` now holds the shared signed publication/transport/
payload verification previously inside the retriever. It reconstructs the
expected unwrap request from the current grant, selected catalog index, role,
verified Merkle membership, registered wrapping key, exact ciphertext/wrapped
key and AAD commitments. The retriever uses the same reconstruction and now
checks membership on direct reads as well as metadata descriptions.

`PrivateV2Custody` consults its independently configured worker's PostgreSQL
grant store before key access and again after decrypting. It requires exact
request equality, including evaluation/attempt/release identity, phase,
audience, expiry and frozen patch. Revocation, expiry, retirement, ownership
drift and a phase transition prevent key release. No request can select another
catalog entry, wrapped key, private key file or worker identity. Unknown JSON
fields are non-authoritative and are not echoed or included in the digest.

The concrete protected-file backend accepts RSA-3072 through RSA-8192 with
exponent 65537, requires its SPKI fingerprint to match the registered transport,
and uses OAEP-SHA256 with the registered AAD hash as label. It reads the private
key only after authorization. It returns exactly one 32-byte AES data key plus
the native request digest; it is not a general RSA decryption service.

## Separate process and Unix identity

Run the custody service as a dedicated non-root custodian UID, different from
the worker UID. Its private key, database credential allowlist and configuration
are owner-only mode-0600 files beneath canonical mode-0700 directories. The
worker receives neither private key nor custody database credentials.

The Unix socket uses a separate custodian-owned mode-0755 directory with safe
ancestors (normally `/run/ditto-coding-custody`). It is mode 0666 to permit the
distinct worker UID to connect without sharing the private-key group. Access
control is the kernel's `SO_PEERCRED`, not possession of the pathname: the
service checks the exact approved client UID **before reading a request**. The
proxy independently checks the server UID before sending private metadata.
Same-UID/root service configurations are refused. Host root remains trusted.
Do not mount the socket into candidate or validator containers. Kernel/filesystem
identity isolation must be qualified on the real host before activation.

Frames are one newline-terminated request (16 KiB maximum) and one response
(1 KiB maximum), with four active authorized requests and a 20-second transport
deadline. The grant is rechecked before any key leaves the service. Blocking
local key-file/crypto operations are not preempted by an asyncio timeout, so
expiry is checked again afterward; provisioning must use healthy local storage.
The worker's process adapter also retains its independent subprocess deadline.
Revocation cannot erase a key already delivered under a valid earlier grant.

Errors close the connection without object details or key bytes. Startup never
unlinks an existing socket. Shutdown stops acceptance, cancels/drains active
requests, then closes the database; it removes only its own socket inode, never
keys, authority files or private state. This is not evidence recovery, private
attempt replay, crash reconciliation, remote KMS attestation or memory zeroization.

## Explicit invocation

The operator provides a protected configuration with schema
`dittobench-coding-private-v2-custody-config-v1`, `shadow_only=true`,
`weight_eligible=false`, and these fields:

- `worker_id`: exact assigned Platform worker UUID, never supplied by a request.
- `client_uid`: approved worker OS UID, different from the custody process UID.
- `socket_path`: absolute socket path in the separately provisioned IPC directory.
- `registration_file`, `transport_manifest`, `payload_authority`,
  `publication_receipt`, `curator_public_key`: exact approved protected authority
  files, including the independently trusted curator public key.
- `reader_authority_sha256`: independently approved Hippius reader authority
  fingerprint. No Hippius credential is needed by custody.
- `private_key_file`: protected private RSA key matching the registered wrapping
  fingerprint; never put this key in the worker's configuration or image.
- `postgres_environment_file`: the existing native runtime's bounded JSON
  `POSTGRES_*=value` allowlist. Grant reads use the existing row-lock order.

Start only as the approved custodian:

```text
<protected-python> -I -m ditto.coding_private_v2_custody --serve-custody --config <private-config>
```

The separately installed worker-owned helper selected by the existing runtime
can invoke the credential-free proxy with a fixed socket and custodian UID:

```text
<protected-python> -I -m ditto.coding_private_v2_custody --proxy-once --socket <fixed-socket> --custodian-uid <approved-uid>
```

It forwards the existing canonical request on stdin and the bounded response on
stdout. The worker's `ProcessPrivateV2Unwrapper` still verifies response binding,
key length and expiry. CLI modes are mutually exclusive; no mode is default.
Server configuration, identities, key custody and installation require explicit
operator approval. No helper installation or live invocation is performed by CI.

## Evidence and remaining gates

Tests exercise real RSA/AES retrieval, request-field tampering before key access,
PostgreSQL-controlled freeze/revocation, expiry during unwrap, unsafe key modes,
Merkle drift, real Unix unauthorized-peer rejection, and a real socket roundtrip
with simulated distinct credentials. The last test is explicitly not proof of
cross-UID host isolation. Live peer/cleanup/resource qualification and approved
service installation remain required before a private shadow canary. Hippius
remains the sole remote store for private inputs and sealed evidence.
