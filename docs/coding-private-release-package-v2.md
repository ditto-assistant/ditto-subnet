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

## Visible snapshot capsule

The `visible_bundle` is an uncompressed tar of a sanitized snapshot v2 capsule:
one canonical `manifest.json` plus its declared files below `workspace/`.
The repository root used by execution is the validated `workspace/` directory,
not the enclosing capsule. A package must not guess between alternative roots.

Preparation binds the snapshot manifest to the frozen group manifest and checks
its declared paths, lengths, hashes, normalized modes and tree identity against
the archive. Verification repeats these checks after reading the stored payload.
Missing/extra files, duplicate paths, links, unsafe paths and mode drift fail
closed. Keeping a parent directory private does not justify changing normalized
workspace modes after snapshot export; confidentiality belongs to protected
staging and encryption, while the capsule manifest binds execution metadata.

The offline curator alone may invoke the existing encryption and publication
commands. Platform, validators, miners, executors, and models receive no
curator credential, wrapping private key, or reusable Hippius capability.

This layer deliberately reuses the established Hippius private-input transport
instead of adding a second object-store format or fallback provider.
