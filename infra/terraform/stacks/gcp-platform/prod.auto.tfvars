# Reviewed, non-secret production intent. Terraform loads this file
# automatically in local and GitHub Actions plans so an omitted CLI flag cannot
# silently propose destroying an already-managed optional service.

manage_dns           = true
enable_datapipeline  = true
enable_embedder      = true
enable_validator     = true
enable_screener      = true
enable_screener_prod = true

# The fleet and its secret/IAM phase already exist in production. The
# Targon-first controller starts with its hostile-runtime capability pinned to
# NOGO, so real submission demand continues to use the bounded GCE fallback.
enable_screener_fleet_secrets       = true
enable_screener_fleet               = true
enable_screener_capacity_controller = true

screener_fleet_min_replicas         = 0
screener_fleet_max_replicas         = 6
screener_fleet_backlog_per_instance = 6

# App VMs share one boot disk size. 30G filled ditto-platform-prod during
# git fetch (uv cache + unbounded pm2 logs + relay trace spool).
app_boot_disk_gb = 100
