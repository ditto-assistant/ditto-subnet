.PHONY: lint format typecheck test py-test go-test go-lint harness-build

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy ditto/

# Default test target runs Python + Go suites so contributors can `make test`
# once and know both halves of the parity contract still hold.
test: py-test go-test

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
