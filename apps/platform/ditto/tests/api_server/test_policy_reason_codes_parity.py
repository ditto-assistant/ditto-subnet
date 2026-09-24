"""The pinned reason-code catalog is exactly policy-v13.md's published list."""

import re
from pathlib import Path

from ditto_screening_protocol import POLICY_V13_REASON_CODES

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
