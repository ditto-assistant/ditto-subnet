# Debian13 x86_64 userland QA only; not the GCE guest image or live IAM proof.
FROM debian:13-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl git procps python3 python3-venv && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["python3", "/fixture/source/infra/terraform/tests/collector-custody/bootstrap-qa.py"]
