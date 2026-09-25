"""Run one exact-artifact, non-authoritative Sol pilot review locally."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ditto_screener.sol_report_candidate import Identity, run_report_candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    key = args.key_file.read_text(encoding="utf-8").strip()
    if not key.startswith("sk-or-"):
        raise ValueError("OpenRouter key file is missing or invalid")
    report = asyncio.run(
        run_report_candidate(
            args.archive,
            identity=Identity(args.artifact_sha256, 13, args.agent_id, args.attempt_id),
            api_key=key,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    print(json.dumps({"authority": report["authority"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
