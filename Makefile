.PHONY: lint format typecheck test smoke-pylon

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
	uv run python scripts/smoke_pylon.py
