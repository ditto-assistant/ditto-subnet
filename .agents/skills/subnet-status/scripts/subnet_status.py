#!/usr/bin/env python3
"""Print SN118's live state as a miner sees it, from public endpoints only.

Usage:
    python3 .agents/skills/subnet-status/scripts/subnet_status.py
    python3 .agents/skills/subnet-status/scripts/subnet_status.py --hotkey <ss58>
    python3 .agents/skills/subnet-status/scripts/subnet_status.py --json

No credentials, no writes. Standard library only, so it runs anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

DEFAULT_BASE = "https://dittobench.ai/api/v1/public"
TIMEOUT_SECONDS = 20


def fetch(base: str, path: str) -> Any:
    """GET one public endpoint; return None (and say why) instead of crashing."""
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    # An explicit User-Agent: the CDN refuses urllib's default one.
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "sn118-subnet-status/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"  ! {path}: {error}", file=sys.stderr)
        return None


def pct(value: Any) -> str:
    return f"{float(value) * 100:.1f}%" if isinstance(value, (int, float)) else "?"


def collect(base: str, hotkey: str | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rollout": fetch(base, "bench/rollout"),
        "bench_config": fetch(base, "bench/config"),
        "treasury": fetch(base, "treasury-allocation"),
        "leaderboard": fetch(base, "leaderboard?limit=10"),
        "weights": fetch(base, "weights"),
    }
    if hotkey:
        miner = fetch(base, f"miners/{hotkey}")
        snapshot["miner"] = miner
        submissions = (miner or {}).get("submissions") or []
        newest = sorted(
            submissions, key=lambda row: row.get("created_at") or "", reverse=True
        )[:5]
        snapshot["agents"] = [
            fetch(base, f"agent/{row['agent_id']}/summary")
            for row in newest
            if row.get("agent_id")
        ]
    return snapshot


def render(snapshot: dict[str, Any], hotkey: str | None) -> None:
    rollout = snapshot.get("rollout") or {}
    harness = (snapshot.get("bench_config") or {}).get("harness") or {}
    print("== Benchmark")
    print(
        f"  scoring bench_version {rollout.get('active_version', '?')}"
        f" (desired {rollout.get('desired_version', '?')},"
        f" rollout {rollout.get('status', '?')})"
    )
    if harness:
        print(
            f"  harness model {harness.get('canonical_id', '?')},"
            f" locked={harness.get('locked')},"
            f" reasoning_effort={harness.get('reasoning_effort')}"
        )

    treasury = snapshot.get("treasury") or {}
    miner_share = treasury.get("forecast_miner_share")
    print("== Emission split of the miner pool (public forecast)")
    print(
        f"  Gamma (service) {pct(treasury.get('forecast_service_share'))}"
        f"  burn {pct(treasury.get('forecast_burn_share'))}"
        f"  miners {pct(miner_share)}"
        f"  [policy revision {treasury.get('policy_revision', '?')},"
        f" routing {treasury.get('routing_status', '?')}]"
    )

    board = snapshot.get("leaderboard") or {}
    emissions = board.get("emissions") or {}
    print("== Competition: memory KOTH")
    if emissions:
        curve = emissions.get("rank_shares") or []
        print(
            "  payout curve "
            + "/".join(f"{share * 100:g}" for share in curve)
            + f"  dethrone margin {emissions.get('margin')}"
            + f"  z={emissions.get('dethrone_z')}"
        )
        print(
            f"  champion {emissions.get('champion_agent_id')}"
            f" ({emissions.get('champion_miner_hotkey')})"
        )
        for recipient in emissions.get("recipients") or []:
            share = recipient.get("share_of_miner_pool")
            of_all = (
                f" = {pct(share * miner_share)} of all miner emission"
                if isinstance(share, (int, float))
                and isinstance(miner_share, (int, float))
                else ""
            )
            print(
                f"    {recipient.get('role', '?'):<14} rank {recipient.get('raw_rank')}"
                f"  {recipient.get('miner_hotkey')}  {pct(share)} of KOTH{of_all}"
            )
    entries = board.get("entries") or []
    if entries:
        print("  top of the board:")
        for entry in entries[:10]:
            print(
                f"    #{entry.get('rank')}  {entry.get('agent_name')}"
                f" v{entry.get('agent_version')}"
                f"  composite {entry.get('composite')}  {entry.get('miner_hotkey')}"
            )

    epoch = (snapshot.get("weights") or {}).get("epoch") or {}
    if epoch:
        print("== Epoch")
        print(
            f"  next weight epoch at {epoch.get('next_epoch_at')}"
            f" ({epoch.get('blocks_until_next_epoch')} blocks;"
            f" tempo {epoch.get('tempo_blocks')})"
        )

    if hotkey:
        print(f"== Your agents ({hotkey})")
        ranks = {entry.get("agent_id"): entry.get("rank") for entry in entries}
        for agent in snapshot.get("agents") or []:
            if not agent:
                continue
            print(
                f"  {agent.get('name')} v{agent.get('version')}"
                f"  {agent.get('agent_id')}"
            )
            print(
                f"    status {agent.get('status')}"
                f"  scores {agent.get('score_count')}/{agent.get('quorum')}"
                f"  composite {agent.get('score_composite')}"
                f"  board rank {ranks.get(agent.get('agent_id'), '-')}"
            )
            for key in ("screening_reason", "review_reason", "review_conclusion"):
                if agent.get(key):
                    print(f"    {key}: {agent[key]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--hotkey", help="your miner hotkey (ss58) to list your agents")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--json", action="store_true", help="print the raw snapshot")
    args = parser.parse_args()
    snapshot = collect(args.base_url, args.hotkey)
    if args.json:
        json.dump(snapshot, sys.stdout, indent=2, default=str)
        print()
    else:
        render(snapshot, args.hotkey)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
