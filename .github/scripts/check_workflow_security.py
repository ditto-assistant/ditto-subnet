#!/usr/bin/env python3
"""Fail CI when workflow changes weaken the repository trust boundary."""

import re
import sys
from pathlib import Path

ROOTS = (Path(".github/workflows"),)
TRIGGERS = {
    "pull_request_target",
    "workflow_run",
    "issue_comment",
    "repository_dispatch",
}
TRIGGER_RE = re.compile(r"^\s*(" + "|".join(sorted(TRIGGERS)) + r")\s*:")
USES_RE = re.compile(r"\buses:\s*([^\s#]+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PR_TARGET_ALLOWLIST = {"preview-stack.yml"}


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
                    allowed = (
                        trigger.group(1) == "pull_request_target"
                        and path.name in PR_TARGET_ALLOWLIST
                    )
                    if not allowed:
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
            if path.name in PR_TARGET_ALLOWLIST:
                contents = path.read_text()
                required = (
                    "environment: preview-stack",
                    "ref: ${{ github.event.repository.default_branch }}",
                    "persist-credentials: false",
                    "github.event.pull_request.head.repo.full_name",
                )
                for marker in required:
                    if marker not in contents:
                        failures.append(
                            f"{path}: trusted preview controller is missing {marker!r}"
                        )
                if (
                    "actions/checkout" in contents
                    and "ref: ${{ github.event.pull_request.head.sha }}" in contents
                ):
                    failures.append(
                        f"{path}: privileged preview controller checks out PR code"
                    )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("workflow security policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
