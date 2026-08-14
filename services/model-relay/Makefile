# services/model-relay — Go replacement for the Python DITTO_ROLE=relay
# process. See README.md.

GENERATED := internal/postgres/models.go internal/postgres/db.go \
	internal/postgres/*.sql.go db/schema.sql

.PHONY: build vet test fmt-check gen-schema sqlc-generate sqlc-check release-build

build:
	go build ./...

vet:
	go vet ./...

test:
	go test ./...

fmt-check:
	@out="$$(gofmt -l .)"; if [ -n "$$out" ]; then echo "gofmt needed:"; echo "$$out"; exit 1; fi

# Regenerate db/schema.sql from the real apps/platform Alembic chain
# (requires Docker; see scripts/gen-schema.sh).
gen-schema:
	scripts/gen-schema.sh

# Regenerate schema + sqlc output (mirrors the backend's sqlc-generate wrapper).
sqlc-generate: gen-schema
	go tool sqlc generate -f internal/postgres/sqlc.yaml

# Drift check for CI: regeneration must be a no-op.
sqlc-check: sqlc-generate
	git diff --exit-code -- $(GENERATED)

# The release artifact: statically linked linux/amd64 binary.
release-build:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o model-relay ./cmd/model-relay
