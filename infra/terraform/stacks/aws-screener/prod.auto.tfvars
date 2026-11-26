# DITTO-2594: reviewed production intent for one on-demand AWS screener.
# Apply gcp-platform's screener-aws federation before applying this root.
enable_aws_screener = true

# v0.484.2 also installs and explicitly invokes Buildx on Debian 12 (#3544).
# Builder digest resolved from this release's exact SHA tag.
# Later releases use the signed host updater.
screener_fleet_release_sha      = "6ba71d343623b354db972fc7a749ac4b1c6ecbc4"
screener_fleet_builder_image    = "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/submission-builder@sha256:2eec6cd63ee1f2c0457a1f497cf18d51a5b5ca15705d6a80e9f5589998278fc5"
screener_fleet_aws_wif_provider = "projects/22790208601/locations/global/workloadIdentityPools/screener-aws/providers/aws-ec2"

screener_fleet_idle_exit_seconds = 900
screener_fleet_worker_processes  = 1
