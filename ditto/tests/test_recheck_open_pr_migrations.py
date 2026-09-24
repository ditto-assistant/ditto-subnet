"""Exercise the main-push migration sweep's commit-status publishing boundary."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "apps/platform/scripts/recheck_open_pr_migrations.sh"


def test_sweep_posts_only_new_or_changed_statuses(tmp_path: Path) -> None:
    mock = tmp_path / "mock-command"
    mock.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import sys

            command = Path(sys.argv[0]).name
            args = sys.argv[1:]
            if command == "git":
                sys.exit(0)
            if command == "python3":
                if "--head" in args:
                    print("mainhead")
                    sys.exit(0)
                failed_refs = (
                    "refs/pr/101", "refs/pr/103", "refs/pr/104", "refs/pr/660"
                )
                if any(ref in args for ref in failed_refs):
                    print("two Alembic heads")
                    sys.exit(1)
                print("one Alembic head")
                sys.exit(0)
            if "--method" in args:
                endpoint = next(arg for arg in args if arg.startswith("repos/"))
                state = next(arg[6:] for arg in args if arg.startswith("state="))
                if endpoint.endswith("/statuses/sha660"):
                    print(
                        "gh: Validation failed: This SHA and context has reached "
                        "the maximum number of statuses.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                with open(os.environ["MOCK_POSTS"], "a") as output:
                    record = {"endpoint": endpoint, "state": state}
                    output.write(json.dumps(record) + "\\n")
                print("{}")
                sys.exit(0)
            endpoint = next(arg for arg in args if arg.startswith("repos/"))
            if endpoint.endswith("pulls?state=open&per_page=100"):
                print(json.dumps([[{"number": number, "head": {"sha": f"sha{number}"}}
                    for number in (714, 100, 101, 102, 103, 104, 660)]]))
            elif "/files?" in endpoint:
                number = int(endpoint.split("/pulls/")[1].split("/")[0])
                path = ("apps/platform/alembic/versions/new.py" if number in
                    (100, 101, 103, 104, 660) else "README.md")
                print(json.dumps([[{"filename": path}]]))
            elif endpoint.endswith("/status"):
                number = int(endpoint.split("/commits/sha")[1].split("/")[0])
                states = {
                    714: (
                        "success",
                        "Adds no migration; cannot fork the Alembic chain.",
                    ),
                    100: (
                        "success",
                        "Merging into main leaves exactly one Alembic head.",
                    ),
                    101: (
                        "success",
                        "Merging into main leaves exactly one Alembic head.",
                    ),
                    103: (
                        "failure",
                        "Merging into main (oldhead) leaves multiple Alembic heads.",
                    ),
                    104: (
                        "failure",
                        "Merging into main (mainhead) leaves multiple Alembic heads.",
                    ),
                    660: (
                        os.environ.get("MOCK_660_STATUS_STATE", "failure"),
                        "Merging into main would leave more than one Alembic head.",
                    ),
                }
                print("\\t".join(states[number]) if number in states else "")
            else:
                raise SystemExit(f"unexpected gh endpoint: {endpoint}")
            """
        )
    )
    mock.chmod(0o755)
    for name in ("gh", "git", "python3"):
        (tmp_path / name).symlink_to(mock)

    posts = tmp_path / "posts.jsonl"
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MOCK_POSTS": str(posts),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert [json.loads(line) for line in posts.read_text().splitlines()] == [
        {
            "endpoint": "repos/ditto-assistant/ditto-subnet/statuses/sha101",
            "state": "failure",
        },
        {
            "endpoint": "repos/ditto-assistant/ditto-subnet/statuses/sha102",
            "state": "success",
        },
        {
            "endpoint": "repos/ditto-assistant/ditto-subnet/statuses/sha103",
            "state": "failure",
        },
    ]
    assert "PR #714: success status unchanged" in result.stdout
    assert "PR #100: success status unchanged" in result.stdout
    assert "PR #104: failure status unchanged" in result.stdout
    assert "PR #660 status capped" in result.stdout
    assert "#101" in summary.read_text()
    assert "#103" in summary.read_text()
    assert "#660" in summary.read_text()

    # A capped success cannot be left in place when the computed result is red.
    stale_status = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MOCK_POSTS": str(posts),
            "MOCK_660_STATUS_STATE": "success",
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert stale_status.returncode == 1
    assert "maximum number of statuses" in stale_status.stderr
