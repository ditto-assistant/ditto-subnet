"""Run one exact-artifact, non-authoritative Sol pilot review locally."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from ditto_screener.sol_report_candidate import Identity, run_report_candidate


def _write_private_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


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
    identity = Identity(args.artifact_sha256, 13, args.agent_id, args.attempt_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    progress_path = args.output.with_name(args.output.name + ".progress.json")
    progress: dict[str, object] = {
        "authority": "none",
        "identity": identity.__dict__,
        "status": "running",
        "phases": {},
    }

    def record_progress(snapshot: dict[str, object]) -> None:
        phases = progress["phases"]
        assert isinstance(phases, dict)
        phases[snapshot["phase"]] = snapshot
        _write_private_json(progress_path, progress)

    try:
        report = asyncio.run(
            run_report_candidate(
                args.archive,
                identity=identity,
                api_key=key,
                on_progress=record_progress,
            )
        )
    except Exception as exc:
        progress["status"] = "failed"
        progress["error_type"] = type(exc).__name__
        _write_private_json(progress_path, progress)
        raise
    _write_private_json(args.output, report)
    progress["status"] = "completed"
    progress["total_usage"] = report["total_usage"]
    _write_private_json(progress_path, progress)
    print(json.dumps({"authority": report["authority"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
