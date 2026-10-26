"""Show a competition's submission terms before an upload.

Platform serves ``GET /public/submission-terms/{competition}``: the
deliverable (artifact kind, contract version, required files, every
upload-time limit and where it comes from), the upload cooldown, and the
competition's own fee. The upload commands print it before any payment so a
miner sees what they are submitting and what it costs. It is informational:
Platform enforces every term itself, so a missing or unreadable terms
response never blocks an upload.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any, Protocol

from ditto.miner_cli.errors import MinerCliError

_RAO_PER_TAO = Decimal(1_000_000_000)


class _TermsClient(Protocol):
    def get_submission_terms(self, competition: str) -> dict[str, Any] | None: ...


def _format_value(limit: dict[str, Any]) -> str:
    value = limit.get("value")
    unit = limit.get("unit")
    if unit == "bytes" and isinstance(value, int):
        if value % (1 << 20) == 0:
            return f"{value >> 20} MiB"
        if value % (1 << 10) == 0:
            return f"{value >> 10} KiB"
        return f"{value} bytes"
    if unit in {"files", "lines", "characters", "cases"}:
        return f"{value} {unit}"
    return str(value)


def format_submission_terms(terms: dict[str, Any]) -> list[str]:
    """Human-readable lines for one competition's terms."""
    deliverable = terms.get("deliverable") or {}
    fee = terms.get("fee") or {}
    cooldown = terms.get("cooldown") or {}
    lines = [f"Submission terms: {terms.get('competition')} competition"]
    kind = deliverable.get("kind")
    contract = deliverable.get("contract_version")
    lines.append(f"  Artifact: {kind}" + (f" ({contract})" if contract else ""))
    if deliverable.get("format"):
        lines.append(f"  Format:   {deliverable['format']}")
    required = deliverable.get("required_files") or []
    if required:
        lines.append(f"  Requires: {', '.join(str(name) for name in required)}")
    for limit in deliverable.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        lines.append(
            f"  Limit:    {limit.get('name')} = {_format_value(limit)} "
            f"[{limit.get('source')}: {limit.get('reference')}]"
        )
    if cooldown:
        lines.append(
            f"  Cooldown: {cooldown.get('seconds')} s per {cooldown.get('scope')}"
        )
    amount = fee.get("amount_rao")
    if isinstance(amount, int) and not isinstance(amount, bool):
        price = f"{Decimal(amount) / _RAO_PER_TAO} TAO ({amount} rao)"
        source = (
            "this competition's price"
            if fee.get("amount_source") == "override"
            else "the default submission fee"
        )
        if fee.get("required"):
            lines.append(f"  Fee:      {price}, {source}, required")
        else:
            lines.append(f"  Fee:      none required (price if enabled: {price})")
    if terms.get("accepting") is False:
        lines.append("  Status:   not accepting submissions")
    return lines


def show_submission_terms(
    client: _TermsClient, competition: str
) -> dict[str, Any] | None:
    """Print ``competition``'s terms to stderr and return them.

    Returns ``None`` (printing nothing) on a Platform that predates the
    endpoint, and warns instead of failing when it cannot be read.
    """
    try:
        terms = client.get_submission_terms(competition)
    except MinerCliError as error:
        print(f"note: could not read submission terms: {error}", file=sys.stderr)
        return None
    if not isinstance(terms, dict):
        return None
    if terms.get("competition") != competition:
        raise MinerCliError(
            f"platform returned the {terms.get('competition')!r} competition's "
            f"terms for a {competition} upload; no payment was sent"
        )
    print("\n".join(format_submission_terms(terms)), file=sys.stderr)
    return terms
