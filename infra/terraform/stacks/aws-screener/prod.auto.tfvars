# DITTO-2594: reviewed production intent for one on-demand AWS screener.
# Apply gcp-platform's screener-aws federation before applying this root.
enable_aws_screener = true

# v0.484.1 includes the fresh-host and rootless stop/wake repairs (#3540/#3541).
# Builder digest resolved from this release's exact SHA tag.
# Later releases use the signed host updater.
screener_fleet_release_sha      = "a372e8eea770a74eb9ffa04cbb7a2c35c54a67c7"
screener_fleet_builder_image    = "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/submission-builder@sha256:898cfce75fb735efeff501dcd51d60ad290561cca78fcc5b0156edfdcfce7838"
screener_fleet_aws_wif_provider = "projects/22790208601/locations/global/workloadIdentityPools/screener-aws/providers/aws-ec2"

screener_fleet_idle_exit_seconds = 900
screener_fleet_worker_processes  = 1
