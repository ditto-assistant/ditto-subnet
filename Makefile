.PHONY: lint format typecheck test py-test go-test go-lint harness-build parity-smoke

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
