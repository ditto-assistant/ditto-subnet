FROM golang:1.25-alpine@sha256:56961d79ea8129efddcc0b8643fd8a5416b4e6228cfd477e3fd61deb2672c587 AS helper
WORKDIR /src
COPY services/screener-orchestrator/submission-builder/main.go ./main.go
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -trimpath -ldflags='-s -w -buildid=' -o /out/ditto-submission-builder ./main.go

FROM us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/kaniko-executor@sha256:9ccc06f5e9232131df62187de0bd1dafe03f959b0ff547e6c304984aa83cf2aa
COPY --from=helper /out/ditto-submission-builder /kaniko/ditto-submission-builder
ENTRYPOINT ["/kaniko/ditto-submission-builder"]
