# Reviewed, non-secret production intent. Terraform loads this file
# automatically in local and GitHub Actions plans so an omitted CLI flag cannot
# silently propose destroying an already-managed optional service.

manage_dns            = true
enable_datapipeline   = true
enable_embedder       = true
enable_validator      = true
enable_validator_prod = true
# The generator VM must never become persistent production intent. The
# protected plan workflow overrides this only for a supervised bootstrap/armed
# window, then seals a teardown plan returning it to absent.
validator_hotkey_admin_phase = "absent"
enable_screener              = true
# The static ditto-screener-prod pet is retired. Hetzner is primary and the
# independently managed GCE MIG remains the bounded overflow path.
enable_screener_prod = false

# The fleet and its secret/IAM phase already exist in production. The
# Targon-first controller starts with its hostile-runtime capability pinned to
# NOGO, so real submission demand continues to use the bounded GCE fallback.
enable_screener_fleet_secrets       = true
enable_screener_fleet               = true
enable_screener_capacity_controller = true
# Rehearsal VM is opt-in through the protected workflow and absent otherwise.
enable_screener_fleet_dev_host = false
# The bare-metal X.509 identity is enabled only during a protected apply after
# its public CA trust anchor has been reviewed and supplied.
enable_screener_fleet_x509_identity = false

screener_fleet_min_replicas         = 0
screener_fleet_max_replicas         = 6
screener_fleet_backlog_per_instance = 6

# App VMs share one boot disk size. 30G filled ditto-platform-prod during
# git fetch (uv cache + unbounded pm2 logs + relay trace spool). Provider 6.x
# ForceNew on size: grow the live disks, then pin 100G here.
app_boot_disk_gb = 100
