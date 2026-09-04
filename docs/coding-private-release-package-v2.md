# Coding private release package v2

Status: shadow-only package contract. No package is created, encrypted,
uploaded, or registered by this document.

An audited private release package converts opaque private-group manifests into
the existing client-side Hippius private-input transport. It contains no private
Git metadata, raw provenance, reference patch, or decryption key.

```text
audited group manifests
  -> canonical catalog records
  -> existing AES-GCM/RSA-OAEP transport preparation
  -> offline curator signature
  -> exact-reader publication and readback
```

The package binds one release ID, catalog commitment, group-manifest digests,
audit-report digests, object plaintext/ciphertext digests, wrapping-key digest,
and `weight_eligible=false`. Any missing audit, duplicate group, opaque-ID
collision, unbalanced condition group, or mismatched object digest fails closed.

The offline curator alone may invoke the existing encryption and publication
commands. Platform, validators, miners, executors, and models receive no
curator credential, wrapping private key, or reusable Hippius capability.

This layer deliberately reuses the established Hippius private-input transport
instead of adding a second object-store format or fallback provider.
