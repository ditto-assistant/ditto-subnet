# Reviewed public custody intent; this root owns no Platform or screening resources.
project = "ditto-app-dev"
region  = "us-central1"
zone    = "us-central1-a"
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
