# Reviewed, non-secret production intent. Terraform loads this file
# automatically in local and GitHub Actions plans so an omitted CLI flag cannot
# silently propose destroying an already-managed optional service.

project               = "ditto-app-dev"
manage_dns            = true
enable_datapipeline   = true
enable_embedder       = true
enable_validator      = true
enable_validator_prod = true
# Snapshot metadata only; encrypted-backup custody is in ditto-subnet.
enable_platform_postgres_snapshot_reader = true
# Treasury hosts remain physically absent until separately reviewed applies.
enable_treasury_host         = false
enable_treasury_planner_host = false
# Shadow coding remains physically absent until a separately reviewed protected
# apply creates the complete three-host executor cohort. No worker, daemon, or
# provider route is activated by this default.
coding_executor_host_count = 0
# Native v2 qualification foundation only, with the explicitly nominated custodian.
# Requires a reviewed protected plan/apply; no runtime or private-data authority.
enable_coding_hosted_host     = true
coding_hosted_operators       = ["user:peyton@omniaura.ai"]
enable_coding_hosted_postgres = true
# SN-138: cold snapshot restored and boot/RSA recovery verified. Preparation
# only: keep the VM and Terraform prevent_destroy until a separate removal plan.
coding_hosted_deletion_protection = false
# The generator VM must never become persistent production intent. The
# protected plan workflow overrides this only for a supervised bootstrap/armed
# window, then seals a teardown plan returning it to absent.
validator_hotkey_admin_phase = "absent"
# The static ditto-screener-prod pet is retired. Hetzner is primary and the
# independently managed GCE MIG remains the bounded overflow path.
enable_screener_prod = false

# The fleet and its secret/IAM phase already exist in production. Hetzner is
# primary, with the independently managed GCE MIG as bounded overflow.
enable_screener_fleet_secrets       = true
enable_screener_fleet               = true
enable_screener_capacity_controller = true
# Rehearsal VM is opt-in through the protected workflow and absent otherwise.
enable_screener_fleet_dev_host = false
# The bare-metal X.509 identity is live on subnet-screener-1. Preserve its
# pool, provider, service account, and one-secret grants on routine plans.
enable_screener_fleet_x509_identity       = true
enable_screener_fleet_x509_node2_identity = false

screener_fleet_min_replicas         = 0
screener_fleet_max_replicas         = 6
screener_fleet_backlog_per_instance = 6

# App VMs share one boot disk size. 30G filled ditto-platform-prod during
# git fetch (uv cache + unbounded pm2 logs + relay trace spool). Provider 6.x
# ForceNew on size: grow the live disks, then pin 100G here.
app_boot_disk_gb = 100
