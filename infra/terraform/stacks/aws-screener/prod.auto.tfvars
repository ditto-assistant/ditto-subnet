# DITTO-2594: reviewed production intent for one on-demand AWS screener.
# Apply gcp-platform's screener-aws federation before applying this root.
enable_aws_screener = true

# v0.483.1, resolved from the semantic-release tag; the builder's sha tag
# resolves to this digest. Subsequent releases use the signed host updater.
screener_fleet_release_sha      = "bc5b80466eae95f9a109d2919629d79d9c7b4e9a"
screener_fleet_builder_image    = "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/submission-builder@sha256:1784db4f2012fa02c2b869a45ba971344f4d953fc2a6555c535d6b2ef82f522c"
screener_fleet_aws_wif_provider = "projects/22790208601/locations/global/workloadIdentityPools/screener-aws/providers/aws-ec2"

screener_fleet_idle_exit_seconds = 900
screener_fleet_worker_processes  = 1
