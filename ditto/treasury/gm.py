"""Strict, read-only GM credit balance parsing for treasury reconciliation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class CreditBalance:
    nano_usd: int
    usd: str


def parse_credit_balance(raw: bytes) -> CreditBalance:
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("object") != "credit_balance":
        raise ValueError("unexpected GM credit response")
    balance = body.get("balance")
    if not isinstance(balance, dict):
        raise ValueError("GM credit balance is missing")
    nano = balance.get("nano_usd")
    usd = balance.get("usd")
    if not isinstance(nano, int) or isinstance(nano, bool) or nano < 0:
        raise ValueError("invalid GM nano-USD balance")
    if not isinstance(usd, str) or re.fullmatch(r"[0-9]+\.[0-9]{9}", usd) is None:
        raise ValueError("invalid GM USD balance")
    dollars, nanos = usd.split(".")
    if int(dollars) * 1_000_000_000 + int(nanos) != nano:
        raise ValueError("GM USD and nano-USD balances disagree")
    return CreditBalance(nano_usd=nano, usd=usd)


def read_credit_balance(api_key: str) -> CreditBalance:
    if not api_key:
        raise ValueError("GM API key is required")
    request = Request(
        "https://api.saygm.com/v1/credits",
        headers={"Authorization": f"Bearer {api_key}", "Cache-Control": "no-store"},
    )
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("GM credit read failed")
        return parse_credit_balance(response.read(4096))
