.PHONY: lint format typecheck test py-test go-test go-lint harness-build validator-build validator-smoke parity-smoke

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy ditto/

# Default test target runs Python + Go suites + the Go/Python parity
# smoke so contributors can `make test` once and know both halves of the
# parity contract still hold.
test: py-test go-test parity-smoke

py-test:
	uv run pytest

go-test:
	cd go && go test ./...

go-lint:
	@echo "==> gofmt -d go harness/go-template"
	@diff=$$(gofmt -d go harness/go-template 2>&1); \
	if [ -n "$$diff" ]; then echo "$$diff"; exit 1; fi
	cd go && go vet ./...
	cd harness/go-template && go vet ./...

harness-build:
	cd harness/go-template && go build ./...

# Build the production-shaped validator binary. Outputs to bin/ at the
# repo root so the file is easy to find and gitignored as a build
# artifact.
validator-build:
	mkdir -p bin
	cd go && go build -o ../bin/ditto-validator ./cmd/validator

# End-to-end smoke for the validator pipeline: builds the in-process
# echo-harness, runs the validator against a tiny sample of public
# fixtures, and asserts the report files were produced. No docker, no
# chain network, just the wire pipeline.
validator-smoke: validator-build
	mkdir -p out/validator-smoke
	cd go && go build -o ../bin/ditto-echo-harness ./cmd/echo-harness
	DITTO_ECHO_HARNESS_BIN=$(PWD)/bin/ditto-echo-harness \
	  ./bin/ditto-validator \
	    --fixtures-root ditto/bench/fixtures \
	    --secret validator-smoke-secret \
	    --self-test \
	    --dry-run \
	    --sample 3 \
	    --report-dir out/validator-smoke
	@test -f out/validator-smoke/miner-self-test-hotkey.json \
	  || (echo "validator-smoke: miner report missing" && exit 1)
	@test -f out/validator-smoke/weights-ditto_core.json \
	  || (echo "validator-smoke: core aggregate missing" && exit 1)
	@echo "validator-smoke: OK"

# Run the Go/Python parity smoke locally. CI does the same comparison
# but uses a structural JSON diff; here we just print the Go output and
# rely on `pytest`'s _parity_smoke import to make sure the Python module
# can be loaded.
parity-smoke:
	cd go && go run ./cmd/parity-smoke > /tmp/ditto-parity-go.json
	uv run python -m ditto.bench.runner._parity_smoke > /tmp/ditto-parity-py.json
	@python3 -c 'import json,sys; a=json.load(open("/tmp/ditto-parity-go.json")); b=json.load(open("/tmp/ditto-parity-py.json")); sys.exit(0 if a==b else 1)' \
	  || (echo "parity-smoke: Go and Python diverged" && exit 1)
	@echo "parity-smoke: OK"
