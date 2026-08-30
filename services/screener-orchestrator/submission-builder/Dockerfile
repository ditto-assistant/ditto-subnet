FROM golang:1.25-alpine@sha256:56961d79ea8129efddcc0b8643fd8a5416b4e6228cfd477e3fd61deb2672c587 AS helper
WORKDIR /src
COPY services/screener-orchestrator/submission-builder/main.go ./main.go
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -trimpath -ldflags='-s -w -buildid=' -o /out/ditto-submission-builder ./main.go

FROM docker:28.3.3-cli-alpine3.22@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 AS docker-cli

FROM us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/kaniko-executor@sha256:9ccc06f5e9232131df62187de0bd1dafe03f959b0ff547e6c304984aa83cf2aa
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=helper /out/ditto-submission-builder /kaniko/ditto-submission-builder
ENTRYPOINT ["/kaniko/ditto-submission-builder"]
