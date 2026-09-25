#!/usr/bin/env python3
"""Rebuild the hashed generator-template n-gram corpus shipped with the screener.

Usage (from ``workers/screener``)::

    uv run python scripts/build_generator_ngram_corpus.py [--check]

Reads the DittoBench generator surface files under
``research/dittobench-datagen`` (a public tree) and writes SHA-256 prefixes of
every informative word n-gram found in their string literals to
``ditto_screener/data/generator_ngram_corpus.json``. The corpus is a
deterministic hashed index of that public surface, not a secrecy boundary: it
is hashed only so a hit is reported as a location-only lead and matched text
never appears in a finding. Rerun this script whenever
``research/dittobench-datagen/{datagen,gen,universe}/*.go`` change (the
bench-version-bump checklist names it). ``--check`` exits non-zero when the
committed corpus is missing hashes the current generator would produce (the
corpus lags the generator) so a bench bump that adds surfaces can regenerate
deliberately.

Bench versions are immutable, so a hash committed once stays derivable
forever; a committed hash the current generator no longer yields means the
corpus was built from something other than the research tree and the strict
test in ``tests/test_catalog_writer_leads.py`` fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ditto_screener import generator_ngrams

REPO_ROOT = Path(__file__).resolve().parents[3]
DATAGEN_ROOT = REPO_ROOT / "research" / "dittobench-datagen"
CORPUS_PATH = (
    Path(__file__).resolve().parents[1]
    / "ditto_screener"
    / generator_ngrams.CORPUS_RESOURCE
)

# User-facing generator surfaces only: request templates and grammars, the
# declarative / chit-chat pools, story oracle phrasings, and world record
# sentences. Grader prose and datagen model prompts are deliberately excluded.
SOURCE_FILES = (
    "datagen/datagen.go",
    "datagen/grammars.go",
    "gen/abstention.go",
    "gen/conversational.go",
    "gen/memory_v2.go",
    "universe/questions.go",
    "universe/story.go",
    "universe/world.go",
)

# Starter-kit surfaces every honest miner legitimately mirrors. Any gram
# derivable from these is subtracted so the corpus names only the generator
# surface the kit does not ship; the kit's own datagen never trips the lead.
PUBLIC_SOURCE_FILES = ("miners/dittobench-starter-kit/src/datagen.rs",)
# Public prose and fixture data hashed line by line rather than as literals.
PUBLIC_TEXT_FILES = (
    "miners/dittobench-starter-kit/README.md",
    "miners/dittobench-starter-kit/fixtures/seed-user/pairs.json",
)


def _read_sources(root: Path, relatives: tuple[str, ...]) -> list[str]:
    sources = []
    for relative in relatives:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"generator surface file missing: {path}")
        sources.append(path.read_text(encoding="utf-8"))
    return sources


def regenerate() -> dict:
    private = generator_ngrams.build_corpus_hashes(
        _read_sources(DATAGEN_ROOT, SOURCE_FILES)
    )
    public = generator_ngrams.build_corpus_hashes(
        _read_sources(REPO_ROOT, PUBLIC_SOURCE_FILES)
    ) | generator_ngrams.build_text_hashes(_read_sources(REPO_ROOT, PUBLIC_TEXT_FILES))
    return generator_ngrams.corpus_document(
        private - public,
        source_files=SOURCE_FILES,
        excluded_public_files=(*PUBLIC_SOURCE_FILES, *PUBLIC_TEXT_FILES),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the committed corpus lags the generator; write nothing",
    )
    args = parser.parse_args(argv)
    document = regenerate()
    rendered = generator_ngrams.dumps_corpus(document)
    if args.check:
        if not CORPUS_PATH.is_file():
            print(f"missing corpus: {CORPUS_PATH}", file=sys.stderr)
            return 1
        committed = generator_ngrams.parse_corpus(
            CORPUS_PATH.read_text(encoding="utf-8")
        )
        current = set(document["hashes"])
        stale = sorted(committed - current)
        missing = sorted(current - committed)
        print(f"committed={len(committed)} current={len(current)}")
        print(f"stale={len(stale)} missing={len(missing)}")
        return 1 if stale or missing else 0
    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CORPUS_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {len(document['hashes'])} hashes to {CORPUS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
