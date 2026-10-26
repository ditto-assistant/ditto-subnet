"""Per-competition submission terms shown before an upload."""

from __future__ import annotations

from typing import Any

import pytest

from ditto.miner_cli.errors import ApiResponseError, MinerCliError
from ditto.miner_cli.submission_terms import (
    format_submission_terms,
    show_submission_terms,
)

_GENERATOR_TERMS: dict[str, Any] = {
    "competition": "generator",
    "accepting": True,
    "deliverable": {
        "kind": "generator_source_archive",
        "format": "gzip-compressed tar of a Go main module",
        "contract_version": "dittobench-generator/v1",
        "required_files": ["go.mod", "*.go (package main, at the root)"],
        "limits": [
            {
                "name": "max_archive_bytes",
                "value": 4 << 20,
                "unit": "bytes",
                "source": "code",
                "reference": "ditto.generator_archive.MAX_ARCHIVE_BYTES",
                "description": "the compressed source archive",
            },
            {
                "name": "cases_per_seed",
                "value": 16,
                "unit": "cases",
                "source": "setting",
                "reference": "generator_competition.cases_per_seed",
                "description": "cases per pack",
            },
        ],
    },
    "cooldown": {"seconds": 3600, "scope": "hotkey"},
    "fee": {
        "required": True,
        "amount_rao": 500_000_000,
        "amount_source": "override",
        "required_source": "competition_settings",
        "send_address": "5Fee",
        "settings_revision": 4,
    },
}


class _Client:
    def __init__(self, terms: object = None, error: Exception | None = None) -> None:
        self.terms = terms
        self.error = error

    def get_submission_terms(self, competition: str) -> Any:  # noqa: ARG002
        if self.error is not None:
            raise self.error
        return self.terms


def test_terms_name_the_artifact_limits_and_the_competition_price() -> None:
    lines = format_submission_terms(_GENERATOR_TERMS)
    text = "\n".join(lines)
    assert "Submission terms: generator competition" in text
    assert "generator_source_archive (dittobench-generator/v1)" in text
    assert "max_archive_bytes = 4 MiB [code:" in text
    assert "cases_per_seed = 16 cases [setting:" in text
    assert "Cooldown: 3600 s per hotkey" in text
    assert "0.5 TAO (500000000 rao), this competition's price, required" in text


def test_free_competition_still_shows_the_price_it_would_charge() -> None:
    terms = {
        **_GENERATOR_TERMS,
        "fee": {
            **_GENERATOR_TERMS["fee"],
            "required": False,
            "amount_source": "default",
        },
    }
    assert any(
        "none required (price if enabled: 0.5 TAO" in line
        for line in format_submission_terms(terms)
    )


def test_show_prints_and_returns_terms(capsys: pytest.CaptureFixture[str]) -> None:
    assert show_submission_terms(_Client(_GENERATOR_TERMS), "generator") is not None
    assert "dittobench-generator/v1" in capsys.readouterr().err


def test_an_older_platform_or_an_unreadable_response_never_blocks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert show_submission_terms(_Client(None), "generator") is None
    assert capsys.readouterr().err == ""
    assert (
        show_submission_terms(
            _Client(error=ApiResponseError("submission terms failed: HTTP 503")),
            "generator",
        )
        is None
    )
    assert "could not read submission terms" in capsys.readouterr().err


def test_terms_for_another_competition_are_refused() -> None:
    with pytest.raises(MinerCliError, match="no payment was sent"):
        show_submission_terms(_Client(_GENERATOR_TERMS), "memory")
