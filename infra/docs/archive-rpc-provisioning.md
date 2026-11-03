# Dedicated Finney archive credential

The `gcp-platform` Terraform root owns the empty
`google_secret_manager_secret.subtensor_archive_rpc_api_key` container for
`platform-subtensor-archive-rpc-api-key`. The existing `platform_access` and
`platform_api_access` loops grant its two runtime identities
`roles/secretmanager.secretAccessor`. Provider key values never enter Terraform
variables, resources, state, plans, GitHub variables, or source control.

Merge alone creates nothing. The next apply of this root includes the container
and access bindings, so review the saved plan before applying it through the
protected `infra-plan-apply.yml` workflow, following [CI bootstrap](ci-bootstrap.md).
Do not create an unmanaged container or IAM grant as a shortcut. The intended
change is one protected secret container and its two accessor bindings; reject
unrelated destructive changes in a live plan.

After an authorized apply, add the selected provider key using a private file
or stdin, set the key-free GitHub `prod` Environment URL and auth mode, and
deploy the intended exact Platform release. An empty container does not select
a provider, change source-release policy, or grant a subscription. A populated
dedicated key takes precedence over Taostats key reuse, so coordinate the key
version, URL and auth mode in the same cutover. Before rolling back to Taostats,
remove the dedicated key from the served environment as well as restoring its
URL: `scripts/update.sh` retains the existing key if a secret lookup fails.

Blockmachine's [RPC authentication documentation](https://blockmachine.io/docs/rpc#authentication)
supports `?authorization=<key>` over WebSocket. Platform's existing `query`
mode attaches that parameter at connection time; header-auth support is not
required to select `wss://rpc.blockmachine.io`. Dwellir uses the existing `path`
mode with `wss://api-bittensor-mainnet.n.dwellir.com`. Keep both stored URLs
free of credentials and retain the client's credential redaction.

Re-test historical state and sustained capacity before activation. Then verify
the served provider and credential configuration, sustained collector cursor
progress, and the exact champion payout receipt and 24-hour embargo through
Backroom. RPC success does not prove runtime support or collector catch-up;
unknown runtime fingerprints still fail closed. See the
[Platform archive guide](../../apps/platform/docs/archive-rpc.md).
