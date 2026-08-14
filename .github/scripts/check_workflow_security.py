#!/usr/bin/env python3
"""Fail CI when workflow changes weaken the repository trust boundary."""

import re
import sys
from pathlib import Path

ROOTS = (Path(".github/workflows"), Path(".depot/workflows"))
TRIGGERS = {
    "pull_request_target",
    "workflow_run",
    "issue_comment",
    "repository_dispatch",
}
TRIGGER_RE = re.compile(r"^\s*(" + "|".join(sorted(TRIGGERS)) + r")\s*:")
USES_RE = re.compile(r"\buses:\s*([^\s#]+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    failures: list[str] = []
    for root in ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix not in {".yml", ".yaml", ".disabled"}:
                continue
            for number, raw in enumerate(path.read_text().splitlines(), 1):
                line = raw.split("#", 1)[0]
                trigger = TRIGGER_RE.match(line)
                if trigger:
                    failures.append(
                        f"{path}:{number}: privileged trigger "
                        f"{trigger.group(1)!r} is forbidden"
                    )
                action = USES_RE.search(line)
                if action:
                    target = action.group(1)
                    if not target.startswith(("./", "docker://")):
                        _, separator, revision = target.rpartition("@")
                        if not separator or not SHA_RE.fullmatch(revision):
                            failures.append(
                                f"{path}:{number}: external action must use a full "
                                f"40-character commit SHA: {target}"
                            )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("workflow security policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
