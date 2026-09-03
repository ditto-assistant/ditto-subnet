# Dedicated shadow-coding executor hosts

The optional GCP coding-executor cohort is the physical boundary for a future
shadow coding canary. It is absent by default:

```hcl
coding_executor_host_count = 0
```

The only nonzero value is `3`, matching the future k=3 validator quorum. A
protected Terraform apply may create three private Debian hosts in their own
subnet. They have no public IP, use secure boot/vTPM/integrity monitoring, and
receive a runtime service account with logging and metric permissions only.
That identity has no Secret Manager, Platform, provider, storage, Artifact
Registry, or impersonation permission. IAP/OS Login is scoped to the explicit
executor-operator set and exact VM instances.

The VM-level firewall blocks egress to the Platform and production-validator
subnets. Future rootless candidate execution requires a second, narrower
candidate egress/proxy policy; it is deliberately not implied by this host
foundation.

This Terraform layer does not install Docker, create a rootless daemon, mount a
socket, preload an image, place a wallet, read a secret, start DittoBench, run a
validator, issue a ticket, or enable a coding gate. A later Ansible role must
install the dedicated rootless daemon under an empty local identity, prove its
isolated-daemon label and socket ownership, then keep the worker disabled until
the full Platform/validator/scorer canary is explicitly approved.

Destroying a created cohort is intentionally not a routine rollback: the shared
compute module has deletion protection. Rollback during the shadow phase means
leave the hosts present and disable the later daemon/worker configuration; any
infrastructure teardown requires its own reviewed protected plan.
