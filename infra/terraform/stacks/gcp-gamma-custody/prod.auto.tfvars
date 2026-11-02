project                  = "sn118-gamma-custody"
region                   = "us-central1"
zone                     = "us-central1-a"
enable_collector_custody = true
collector_custody_phases = { registration = "bootstrap", transfer = "bootstrap" }
# Existing reviewed key ceremony supports project-bound metadata guards.
collector_custody_revision = "e1b86b2a673add3572febc35f7159b02e0f8ce37"
collector_custody_operator = "peyton@omniaura.ai"
collector_custody_image    = "projects/debian-cloud/global/images/debian-13-trixie-v20260921"
collector_custody_offline_addresses = [
  "5Fhnko5TCPtR1hyD8aua6n23XRM8cxcBM6TgNEqqUhcxWs1t",
  "5H93a8uVJC6ve2bNVZu9jAGEuBHDFspct3UbHRYGsxUNpA1e",
  "5Cm6Vok2sWkQ33w97AVEsb9U2EyE96Y1utfSLW5vR4viiLKD",
  "5CD8TZWNSNtARxmB5VQF9qTNfCVjhdMdjmwBrniqU3E71ohQ",
  "5F2821EzMcBC2b8hWmrMWiNRcx6DgdaTQQiqmPkbAk7a6yn9",
]
collector_runtime_rpc_egress = false
