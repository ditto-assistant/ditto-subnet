# Read-only post-import native host preflight

This is a prerequisite check within operational gate 2, **not completion of
host qualification**. The first-provisioning daemon check requires zero images;
this separate inspector instead verifies all four approved imported images and
the installed Platform/native runtime. It requires zero containers and a stopped
worker. Unselected images are neither trusted nor removed.

## Preconditions and authority

Use the dedicated Debian 13 amd64 `ditto-coding-hosted-v2` host only, after its
separate provisioning, runtime-install and image-import approvals. Run the
inspector as root from an independently reviewed checkout with root-owned,
non-group/world-writable source files and ancestors. Do not run a writable
developer checkout through sudo. The five inspector/verifier source-file hashes
are retained in the report for independent source/provenance review; these hashes
are not self-authenticating build attestations.

Stage the complete [public release set](coding-native-release-v2.md) under a
canonical root-owned nonwritable-to-others directory. Preserve its relative
layout. The individual artifact files must be root-owned, regular, single-link
and not writable by group/others. No private corpus or credentials are needed.
The original runtime archive is required to verify every installed file and its
exact Debian interpreter/package baseline; an installation sidecar alone is not
sufficient.

The config is root-owned mode 0600 in a root-owned mode 0700 directory. Required
fields are:

| Field | Independently selected input |
|---|---|
| `schema` | `dittobench-coding-native-host-preflight-config-v2` |
| `source_revision` | Exact release-set source commit |
| `release_directory` | Canonical absolute directory holding all five artifacts |
| `release_manifest_sha256` | Reviewed SHA of the release-set index |
| `runtime_archive_sha256` | Independently approved native bundle archive SHA |
| `image_approval_sha256` | Exact `python`, `node`, `go`, `rust` approval-file SHA map |
| `machine_id_sha256` | SHA-256 of the intended host's stripped `/etc/machine-id` bytes |
| `boot_id` | Intended current `/proc/sys/kernel/random/boot_id` UUID |
| `shadow_only`, `weight_eligible` | Explicit `true`, `false` |

Do not populate approvals by blindly hashing newly received files. The custodian
must review the source/provenance and exact bytes, and communicate approved
identities independently. Host identity must likewise be selected from the
intended provisioned instance, not adopted automatically from whatever machine
the command reaches. Reboot requires a newly reviewed boot binding.

```text
/usr/bin/python3 -B -I /ABS/REVIEWED-ROOT-CHECKOUT/infra/scripts/inspect-coding-native-host.py \
  --config /ABS/ROOT-PRIVATE/preflight.json
```

This only prints a bounded, source-free JSON report. It does not write a report
file automatically. Store the result through an approved operator evidence path;
do not publish host identity records as CI artifacts. No configuration, secret,
private input or absolute operator file path is printed. Unknown configuration
fields are ignored and cannot introduce commands, authorities or alternate paths.

## Checks and remaining evidence

The inspector verifies the independently pinned release and archives, complete
installed runtime, host/boot identity, daemon account/subordinate IDs, private
socket/empty client directory, inactive masked rootful Docker units, inactive
worker and active deny guard. Docker diagnostics run with the dedicated daemon
UID/GID, no supplementary groups and a clean fixed environment. Root is used only
for protected reads and systemd/nft inspection, not for a rootful Docker client.

It checks rootless/containerd/cgroup-v2/resource-support metadata, exact imported
repository/config/manifest/profile identities, and an empty container list before
and after inspection. It captures the hash of the dedicated nft table snapshot
and rechecks daemon identity and host boot. Commands have bounded output/time;
they cannot install packages, load images, start/stop services, create containers,
change nft rules or repair state. Python bytecode writes are disabled.

`host_preflight_passed=true` means only the listed checks passed at observation
time. This does not lock the host, exclude changes between observations, prove
hardware integrity or protect against a malicious host administrator. A table
snapshot hash and an active guard service are **not packet-enforcement proof**.
Resource-support metadata is **not measured limit enforcement**.

The report explicitly leaves these host gates pending:

1. Actual worker/candidate network and expiry enforcement, including DNS,
   metadata, private/public, IPv6 and loopback boundaries.
2. Actual CPU, memory, PID and scratch-limit enforcement.
3. Pre-execution candidate confinement through the real language executors.
4. Candidate cleanup and interruption handling on the actual host.

Those require separately approved public probes and subsequent native/private
qualification, not a successful preflight JSON file. `runtime_qualification` and
`private_execution_ready` remain false. Private input/key readiness, recovery,
the complete native matrix, a shadow canary and bounded rollout remain separately
gated. Hippius remains the sole remote private-input/sealed-evidence store, and
`weight_eligible=false` is permanent for this shadow profile.
