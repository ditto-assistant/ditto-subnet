# Reviewed, non-secret production intent. Terraform loads this file
# automatically in local and GitHub Actions plans so an omitted CLI flag cannot
# silently propose destroying an already-managed optional service.

project               = "ditto-app-dev"
manage_dns            = true
enable_datapipeline   = true
enable_embedder       = true
enable_validator      = true
enable_validator_prod = true
# Treasury hosts remain physically absent until separately reviewed applies.
enable_treasury_host         = false
enable_treasury_planner_host = false
# Reviewed custody bootstrap intent. A protected full-root plan and separate
# apply review precede resources; bootstrap grants no secret read/add authority.
# This preserves each phase in source; no runtime, key or financial activation.
enable_collector_custody = true
collector_custody_phases = {
  registration = "bootstrap"
  transfer     = "bootstrap"
}
collector_custody_revision = "e1b86b2a673add3572febc35f7159b02e0f8ce37"
collector_custody_operator = "peyton@omniaura.ai"
# Exact READY/non-deprecated X86_64 image observed before planning; image ID
# 7273087755935743. The former August 17 default has been deprecated by Google.
collector_custody_image = "projects/debian-cloud/global/images/debian-13-trixie-v20260921"
collector_custody_offline_addresses = [
  "5Fhnko5TCPtR1hyD8aua6n23XRM8cxcBM6TgNEqqUhcxWs1t", # Collector coldkey
  "5H93a8uVJC6ve2bNVZu9jAGEuBHDFspct3UbHRYGsxUNpA1e", # Collector hotkey
  "5HeA6bjjt1apDMhLSqKkCWZCEGvZBPByjvMxZ7mKiUk67AGT", # GM holding
  "5CD8TZWNSNtARxmB5VQF9qTNfCVjhdMdjmwBrniqU3E71ohQ", # Bitsec holding
  "5F2821EzMcBC2b8hWmrMWiNRcx6DgdaTQQiqmPkbAk7a6yn9", # Bitcast holding
]
# Shadow coding remains physically absent until a separately reviewed protected
# apply creates the complete three-host executor cohort. No worker, daemon, or
# provider route is activated by this default.
coding_executor_host_count = 0
# Native v2 qualification foundation only, with the explicitly nominated custodian.
# Requires a reviewed protected plan/apply; no runtime or private-data authority.
enable_coding_hosted_host     = true
coding_hosted_operators       = ["user:peyton@omniaura.ai"]
enable_coding_hosted_postgres = true
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
