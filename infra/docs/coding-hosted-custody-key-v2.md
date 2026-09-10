# Native-v2 RSA custody bootstrap

This layer creates one RSA-3072 private-input unwrap identity directly on the
trusted `ditto-coding-hosted-v2` host. It does not create the independent
curator signing key, install or start a custody service, publish private data,
register a release, issue an assignment, or enable scoring, weights or emissions.

The role defaults off and requires the exact confirmation
`BOOTSTRAP NATIVE CODING RSA CUSTODY` plus the reviewed 40-character source
revision. It creates a dedicated locked `ditto-coding-custody` identity distinct
from `ditto-coding-hosted`, two mode-`0700` directories, and four mode-`0600`
single-link files:

- `bootstrap-consumed`, an exclusive one-shot marker written before generation;
- `private-input-rsa.pem`, which never leaves the host;
- `private-input-rsa-public.pem`, which operators may export; and
- `private-input-rsa-receipt.json`, which binds the source revision, host,
  RSA-OAEP-SHA256 profile and public SPKI SHA-256.

The first run refuses any existing key or marker state. A completed rerun
validates the key and requires the receipt to match exactly. Partial state is
retained for explicit reconciliation and is never overwritten or deleted.
The initial production bootstrap exposed a receipt-only encoding defect: the
otherwise correct JSON ended with the two literal bytes `\\n`. A later role may
replace only that exact redacted legacy receipt, only with confirmation
`REPAIR NATIVE CODING RSA RECEIPT`. It verifies the private/public identity
first and never regenerates, rewrites, exports or deletes key material.
The private key is unencrypted because the separately owned custody service
must read it without receiving a passphrase; confidentiality is the trusted
host, dedicated UID, owner-only path and host-root boundary documented in
`apps/platform/docs/coding-private-v2-custody.md`.

Run from the exact reviewed source only after explicit key-operation approval:

```text
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-coding-hosted-custody-key.yml \
  --limit ditto-coding-hosted-v2 \
  -e '{"coding_hosted_custody_key_enabled":true,
       "coding_hosted_custody_source_revision":"<merged-source-sha>",
       "coding_hosted_custody_key_confirmation":"BOOTSTRAP NATIVE CODING RSA CUSTODY",
       "coding_hosted_custody_receipt_repair_confirmation":""}'
```

Export only the public PEM and redacted receipt through the custodian's reviewed
IAP/OS Login session. Never copy the private PEM, add the worker to the custodian
group, expose the key through Secret Manager, or mount the custody directory into
a candidate, validator, model or executor container.

The next gates are an independently controlled Ed25519 curator signer, fresh
transport encryption, external signature, Hippius publication/full readback,
append-only registration, custody service configuration, recovery drills and a
single private shadow canary.
