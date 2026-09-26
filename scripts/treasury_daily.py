"""Scheduled read-only GM credit check and bounded top-up request writer."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ditto.treasury.gm import read_credit_balance
from ditto.treasury.schedule import DailyPolicy, create_daily_request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--gm-api-key-file", type=Path, required=True)
    parser.add_argument("--outbox", type=Path, required=True)
    args = parser.parse_args()
    policy = DailyPolicy(**json.loads(args.policy_file.read_text()))
    key = args.gm_api_key_file.read_text().strip()
    balance = read_credit_balance(key)
    path = create_daily_request(args.outbox, policy, balance, now=datetime.now(UTC))
    print(
        json.dumps(
            {
                "balance_nano_usd": balance.nano_usd,
                "request_path": str(path) if path else None,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
