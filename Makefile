.PHONY: lint format typecheck test smoke-pylon stack-up stack-down migrate migrate-down migrate-history migrate-current

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

smoke-pylon:
	set -a && . ./.env && set +a && uv run python scripts/smoke_pylon.py

stack-up:
	docker compose up -d --wait

stack-down:
	docker compose down

migrate:
	set -a && . ./.env && set +a && uv run alembic upgrade head

migrate-down:
	set -a && . ./.env && set +a && uv run alembic downgrade -1

migrate-history:
	set -a && . ./.env && set +a && uv run alembic history --verbose

migrate-current:
	set -a && . ./.env && set +a && uv run alembic current
