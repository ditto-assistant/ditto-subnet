# Coding datagen guidance

This component is a shadow-only coding-repair corpus compiler. It must not
change DittoBench scores, validator weights, or the active `bench_version`.

Keep public practice data disjoint from evaluation data. Production corpus
bytes, upstream instance identifiers, gold patches, hidden tests, credentials,
and decryption keys must never enter this public tree or a normal build context.

The compiler emits canonical JSON bytes and a complete SHA-256 file manifest.
Runtime consumers verify bytes before parsing them. Do not reconstruct signed
bytes from parsed objects in another language.

The miner-facing view may contain only opaque practice identifiers, one active
user namespace, visible task text, and visible workspace material. Policy
labels, grader tests, reference fixes, source issue URLs, and curator metadata
remain outside that view.

Validate with:

```bash
uv sync --locked --group dev
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run dittobench-coding-datagen validate-pack practice/v1
```
