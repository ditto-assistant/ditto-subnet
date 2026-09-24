"""The pinned reason-code catalog is exactly policy-v13.md's published list."""

import re
from pathlib import Path

from ditto_screening_protocol import (
    FIRST_REASON_CODE_POLICY_VERSION,
    POLICY_V13_REASON_CODES,
    SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
    published_reason_codes,
)

_POLICY = next(
    root / "workers/screener/docs/policy-v13.md"
    for root in Path(__file__).resolve().parents
    if (root / "workers/screener/docs/policy-v13.md").is_file()
)


def _published_codes() -> set[str]:
    text = _POLICY.read_text()
    section = text.split("## Required reason codes", 1)[1]
    block = re.search(r"```text\n(.*?)```", section, re.S)
    assert block is not None, "policy-v13.md lost its reason-code block"
    return {line.strip() for line in block.group(1).splitlines() if line.strip()}


def test_pinned_catalog_matches_the_published_policy_document() -> None:
    assert _published_codes() == set(POLICY_V13_REASON_CODES)


def test_every_activatable_policy_version_publishes_a_reason_code_catalog() -> None:
    """The activation ceiling never outruns the published reason-code catalog.

    The PR that raises ``SCREENING_ACTIVATION_CEILING_POLICY_VERSION`` must also
    publish that policy's reason-code catalog. Otherwise every ATH reject on a
    hold at the new version fails closed with a 422, which is an operator
    lockout.
    """
    missing = [
        version
        for version in range(
            FIRST_REASON_CODE_POLICY_VERSION,
            SCREENING_ACTIVATION_CEILING_POLICY_VERSION + 1,
        )
        if published_reason_codes(version) is None
    ]
    assert missing == []
