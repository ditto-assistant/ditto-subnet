#!/usr/bin/env python3
"""Export public runtime bytes from one exact tracked Git commit, no activation."""

import argparse
import os
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if re.fullmatch(r"[0-9a-f]{40}", args.revision) is None:
        parser.error("requires an exact lowercase commit")
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    if head != args.revision:
        parser.error("revision differs from checked-out commit")
    output = args.output
    if (
        not output.is_absolute()
        or output.resolve() != output
        or output.exists()
        or output.is_relative_to(root)
        or "," in str(output)
    ):
        parser.error("requires a new canonical output directory outside the checkout")
    subprocess.run(["git", "diff-index", "--quiet", "HEAD", "--"], cwd=root, check=True)
    os.umask(0o077)
    with subprocess.Popen(
        ["git", "archive", "--format=tar", args.revision],
        cwd=root,
        stdout=subprocess.PIPE,
    ) as source:
        try:
            subprocess.run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--file",
                    "infra/containers/coding-hosted-runtime.Dockerfile",
                    "--build-arg",
                    "SOURCE_REVISION=" + args.revision,
                    "--target",
                    "export",
                    "--output",
                    "type=local,dest=" + str(output),
                    "-",
                ],
                stdin=source.stdout,
                cwd=root,
                check=True,
            )
        finally:
            assert source.stdout is not None
            source.stdout.close()
        if source.wait() != 0:
            raise RuntimeError("Git source export failed")


if __name__ == "__main__":
    main()
