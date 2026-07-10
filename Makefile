.PHONY: lint format typecheck test prod-up

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

# Start the validator in production under pm2, via the RESI-style auto-updater
# wrapper (scripts/start_validator.py). Requires pm2 (npm i -g pm2), a filled-in
# .env, and Pylon reachable at PYLON_URL. This is the canonical prod one-liner.
prod-up:
	pm2 start "uv run python scripts/start_validator.py" --name ditto_autoupdater
