# Compute module interface

Every `modules/compute/<provider>/` implements one portability contract, so a
host moves between providers by changing the module `source` and a few
provider-placement variables, never its Ansible role or its logical shape.

## Shared inputs

| Input | Type | Meaning |
|---|---|---|
| `name` | string | Logical hostname. GCE instance name; AWS `Name` tag. Ansible inventories use it as `inventory_hostname`. |
| `size` | string | Logical capacity class. Each provider maps it in `locals.tf`. |
| `image` | string | Logical OS image (`debian-13`, `debian-12`, `ubuntu-24`). Each provider resolves the newest x86_64 build. |
| `location` | string | Placement zone: a GCE zone (`us-central1-a`) or an AWS availability zone (`us-east-1a`). |
| `ssh_key_ids` | list(string) | Parity only. Unused on GCP (OS Login) and AWS (SSM Session Manager). |
| `firewall_id` | string | GCP: unused (rules target `network_tags`). AWS: the security group attached to the instance. |
| `user_data` | string | cloud-init user data, or null when Ansible alone configures the host. |
| `labels` | map(string) | GCE labels / AWS tags. Lowercase keys and values, no dots, so one map is valid on both. Inventories group on `env` and `role` and filter on `managed = terraform`. |

## Shared extensions

These have the same name and meaning on every provider that supports them.

| Input | Meaning |
|---|---|
| `subnetwork` | Subnet to attach. GCP: self-link or name (empty uses `default`). AWS: subnet ID (required). |
| `assign_public_ip` | Attach a public IPv4. Ingress stays closed either way. |
| `boot_disk_gb` | Root volume size in GB. |
| `boot_disk_type` | Native disk type (`pd-balanced`; `gp3`). |
| `deletion_protection` | GCE deletion protection / EC2 API termination protection. Default `true`; a retirement flips it in its own reviewed plan first. |

The host identity is provider-native: `service_account_email` on GCP,
`instance_profile_name` on AWS. Both should name a dedicated identity that can
read only what the host needs.

## Shared outputs

`id`, `hostname`, `internal_ip`, `ipv4` (empty without a public IP), `ipv6`
(always empty today).

## Sizes

Add a logical size to every provider map in the same change. Keep the class
comparable (vCPU, memory, shared vs dedicated) and x86_64: generator output
bytes and fingerprints must match miners' x86 machines, and Go fuses FMA on
arm64.

| Logical size | GCP | AWS |
|---|---|---|
| `controller-small` | e2-small | t3.small |
| `app-small` | e2-medium | t3.medium |
| `app-standard`, `db-prod`, `validator` | e2-standard-4 | m6i.xlarge |
| `db-staging` | e2-standard-2 | m6i.large |
| `staging-32gb`, `validator-prod`, `screener-burst` | e2-standard-8 | m6i.2xlarge |
| `screener-burst-n2d` | n2d-standard-8 | m6a.2xlarge |
| `prod-64gb` | e2-standard-16 | m6i.4xlarge |

## Provider differences

- GCP hard-codes `prevent_destroy` because its first callers hold Postgres
  data. The AWS module has no data disk and relies on `deletion_protection`
  for the same two-phase retirement.
- AWS requires IMDSv2 with a hop limit of 1, so a container on the host cannot
  reach instance credentials. Its root volume is always encrypted.
- Hetzner hosts are converged by Ansible against a static inventory. They are
  not a Terraform module; the same Ansible role runs there with a `file` or
  X.509-federated secret source.
