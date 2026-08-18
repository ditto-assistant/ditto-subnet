.PHONY: lint format typecheck test \
        localstack-up localstack-down localstack-bench localstack-smoke localstack-relay-check

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy ditto/

test:
	uv run pytest

# ---- bench localstack (see localstack/README.md) -------------------------------
# Run a REAL scored bench end to end locally against an agent harness, with
# OpenRouter as the only external dependency.

localstack-up:          ## start deps (model relay + dittobench-api). STUB=1 for the free relay.
	./localstack/stack.sh up

localstack-down:        ## stop the deps started by localstack-up
	./localstack/stack.sh down

localstack-bench:       ## one run: AGENT_URL=... BENCH=12 RUN_SIZE=small SEED=42 SCORED=1
	./localstack/bench.sh

localstack-smoke:       ## FREE scored-v12 plumbing smoke against refharness (no OpenRouter)
	./localstack/smoke.sh

localstack-relay-check: ## bounded proof the relay reaches real OpenRouter (locked model)
	./localstack/relay-check.sh
