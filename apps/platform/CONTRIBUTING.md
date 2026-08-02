# Contributing

Thanks for helping improve Ditto Platform.

## Development setup

1. Install Python 3.11 or 3.12, [uv](https://docs.astral.sh/uv/), Node.js 22, and npm.
2. Copy `.env.example` to `.env` and set the required local values described in the README.
3. Run `uv sync --group dev` and `npm ci`.
4. Use the Docker stack when your change needs PostgreSQL, MinIO, or Pylon.

## Before opening a pull request

Run the checks relevant to your change:

```sh
make lint
make typecheck
make test
make lint-copy
```

Keep pull requests focused, explain the behavior change and validation performed, and update documentation when a public contract changes. Database changes must include an Alembic migration.

All changes to `main` go through a pull request, required CI, and code-owner review. Resolve review conversations before requesting merge.

## Security

Do not report vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md) for private reporting.
