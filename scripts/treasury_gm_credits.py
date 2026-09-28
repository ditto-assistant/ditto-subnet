"""Read GM credits and log a non-authorizing reconciliation observation."""

import argparse
import json
import os
from pathlib import Path

from ditto.treasury.gm import read_credit_balance
from ditto.treasury.ledger import append


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    balance = read_credit_balance(os.environ.get("GM_API_KEY", ""))
    entry = append(
        args.ledger,
        event="reconciliation_observation",
        payload={
            "gm_balance_nano_usd": balance.nano_usd,
            "gm_balance_usd": balance.usd,
        },
    )
    print(json.dumps({"balance": balance.usd, "ledger_hash": entry["hash"]}))


if __name__ == "__main__":
    main()
