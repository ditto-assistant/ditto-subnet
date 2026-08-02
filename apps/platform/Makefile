.PHONY: lint lint-copy format typecheck test test-integration test-chain test-db-reset test-db-clean smoke-pylon smoke-api stack-up stack-down migrate migrate-down migrate-history migrate-current api-up embedder-up embedder-down smoke-embedder

lint:
	uv run ruff format --check .
	uv run ruff check .

lint-copy:
	npm run lint:copy

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy ditto/

# The suite runs against a real Postgres in an ambient container the harness
# starts on demand (see ditto/tests/pgharness.py). Nothing to set up first.
test:
	uv run pytest

# Integration tests are part of `make test` now -- they were deselected by
# default, which is why the ditto-platform#438 regression test never ran in
# CI. This target just narrows to them. `-n 0` is no longer required: each
# xdist worker owns its own database, so the TRUNCATE/row-lock inversion
# behind the intermittent DeadlockDetectedError cannot form.
# `not needs_chain` because an explicit `-m` REPLACES the one in addopts, so
# without it this target would also pull in the two tests that need a live
# subtensor behind Pylon. Run those deliberately with `make test-chain`.
test-integration:
	set -a && . ./.env && set +a && uv run pytest -m "integration and not needs_chain"

# The two tests that only a real chain can satisfy. Needs `make stack-up` and a
# reachable SUBTENSOR_NETWORK; not runnable in CI, which is why they are the
# one thing this migration left deselected.
test-chain:
	set -a && . ./.env && set +a && uv run pytest -m needs_chain

# Force a template rebuild. Only needed after editing a migration in place;
# the harness already invalidates on migration-content change.
test-db-reset:
	docker exec ditto-platform-test-postgres psql -U ditto_test -d postgres \
		-c "DROP DATABASE IF EXISTS ditto_test_template"

# Reap every harness-owned database, including any left by a killed run.
test-db-clean:
	docker exec ditto-platform-test-postgres psql -U ditto_test -d postgres -At \
		-c "SELECT datname FROM pg_database WHERE datname LIKE 'ditto_test_%'" \
		| xargs -I{} docker exec ditto-platform-test-postgres \
			psql -U ditto_test -d postgres -c 'DROP DATABASE IF EXISTS "{}"'

smoke-pylon:
	set -a && . ./.env && set +a && uv run python scripts/smoke_pylon.py

api-up:
	set -a && . ./.env && set +a && uv run python -m ditto.api_server

smoke-api:
	set -a && . ./.env && set +a && \
	curl -sf "http://localhost:$${API_PORT:-8000}/health" > /dev/null && echo "api ok"

stack-up:
	# Wait on the long-lived services to report healthy; bring the
	# one-shot bucket-init sidecar up separately because `--wait`
	# treats its (correct) `exited 0` terminal state as not-healthy
	# and fails the whole target.
	docker compose up -d --wait postgres pylon minio
	docker compose up -d minio-create-bucket

stack-down:
	docker compose down

embedder-up:
	# code-embedding service (opt-in `embedder` profile). First boot downloads
	# the model weights (cached in the embedder_hf_cache volume), so it may take a
	# minute to report ready.
	docker compose --profile embedder up -d embedder

embedder-down:
	docker compose --profile embedder down embedder

smoke-embedder:
	# Verify the embedder answers and returns a vector for a snippet of code.
	set -a && . ./.env && set +a && \
	curl -sf "http://localhost:$${CODE_EMBEDDER_HOST_PORT:-8080}/embed" \
		-H 'Content-Type: application/json' \
		-d '{"inputs": "fn main() { println!(\"hi\"); }", "normalize": true}' \
		| head -c 80 && echo " ... embedder ok"

migrate:
	set -a && . ./.env && set +a && uv run alembic upgrade head

migrate-down:
	set -a && . ./.env && set +a && uv run alembic downgrade -1

migrate-history:
	set -a && . ./.env && set +a && uv run alembic history --verbose

migrate-current:
	set -a && . ./.env && set +a && uv run alembic current
