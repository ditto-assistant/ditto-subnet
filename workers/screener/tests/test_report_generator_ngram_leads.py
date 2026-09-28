"""Report-only generator lead scans neither clear nor expose source text."""

from __future__ import annotations

from pathlib import Path

from ditto_screener import generator_ngrams
from scripts import report_generator_ngram_leads
from scripts.report_generator_ngram_leads import report


def test_location_only_report_and_symlink_skip(tmp_path: Path, monkeypatch) -> None:
    phrase = "distinct generator template phrase with several useful tokens"
    hashes = generator_ngrams.hash_text(phrase)
    assert len(hashes) >= 3
    monkeypatch.setattr(generator_ngrams, "load_corpus", lambda: frozenset(hashes))
    # The report module imports the loader directly.
    monkeypatch.setattr(
        "scripts.report_generator_ngram_leads.load_corpus", lambda: frozenset(hashes)
    )
    source = tmp_path / "src"
    source.mkdir()
    (source / "agent.py").write_text(f'prompt = "{phrase}"\n')
    (source / "linked.py").symlink_to(source / "agent.py")
    result = report(tmp_path)
    assert result["authority"] == "none"
    assert result["incomplete"] is True
    assert result["leads"] == [
        {
            "path": "src/agent.py",
            "lines": [1],
            "matched_grams": len(hashes),
            "kind": "fixture-generator-ngram",
        }
    ]
    assert phrase not in str(result)
    assert not any(value in str(result) for value in hashes)


def test_empty_report_is_not_clearance(tmp_path: Path) -> None:
    result = report(tmp_path)
    assert result["authority"] == "none"
    assert result["leads"] == []


def test_scan_limit_marks_report_incomplete(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.py").write_text("pass\n")
    (tmp_path / "b.py").write_text("pass\n")
    monkeypatch.setattr(report_generator_ngram_leads, "MAX_FILES", 1)
    result = report(tmp_path)
    assert result["files_scanned"] == 1
    assert result["incomplete"] is True
