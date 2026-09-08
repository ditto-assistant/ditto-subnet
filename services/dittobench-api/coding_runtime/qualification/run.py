"""Operator-only private compatibility matrix; never a canary or approval.

All receipts stay in an exclusive mode-0700 output directory. Candidate code runs
only in bounded disposable containers, selected by inspected immutable image IDs.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

PROFILES = {
    "python": "python-call-ast-v2",
    "node": "node-call-ast-v2",
    "go": "go-call-ast-v1",
    "rust": "rust-call-ast-v1",
}
PREFIX = "io.heyditto.dittobench."


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def private_json(path, maximum=8 << 20):
    require(path.is_absolute() and path.resolve() == path, "noncanonical private path")
    info = path.lstat()
    require(
        path.is_file() and not info.st_mode & 0o077 and info.st_size <= maximum,
        "private file mode/size rejected",
    )
    return json.loads(path.read_bytes())


def save(root, name, value):
    fd = os.open(
        root / name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        output.write(encoded(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def output(arguments, **kwargs):
    return subprocess.check_output(arguments, timeout=30, **kwargs).decode().strip()


def image_policy(value, language, source):
    config = value["Config"]
    labels = config.get("Labels", {})
    expected_path = "PATH=/usr/local/bin:/usr/bin:/bin"
    if language == "go":
        expected_path = "PATH=/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin"
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", value["Id"]), "image ID rejected")
    require(
        value["Os"] == "linux" and value["Architecture"] == "amd64",
        "image platform rejected",
    )
    require(
        labels.get("org.opencontainers.image.revision") == source,
        "image source does not match matrix",
    )
    require(
        labels.get(PREFIX + "coding-test-driver-profile") == PROFILES[language]
        and labels.get(PREFIX + "coding-supervisor-contract") == "1"
        and labels.get(PREFIX + "coding-supervisor-fixture", "false") == "false",
        "runtime profile or fixture label rejected",
    )
    require(
        config.get("Entrypoint") == ["/usr/local/bin/dittobench-coding-supervisor"]
        and config.get("Env") == [expected_path]
        and not config.get("Volumes")
        and not config.get("Cmd")
        and not config.get("OnBuild"),
        "runtime defaults rejected",
    )
    workdirs = ("/workspace",) if language in ("go", "rust") else ("", "/")
    require(
        config.get("User", "") in ("", "0", "0:0")
        and not config.get("Healthcheck")
        and not config.get("ExposedPorts")
        and config.get("WorkingDir", "") in workdirs,
        "runtime user/healthcheck/workdir rejected",
    )
    return value["Id"]


def envelope(language):
    args = [
        "--network",
        "none",
        "--read-only",
        "--ipc",
        "none",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "KILL",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--cpus",
        "2",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={'384m' if language == 'rust' else '512m'}",
        "--tmpfs",
        "/workspace:rw,noexec,nosuid,nodev,size=64m,mode=0700",
        "--tmpfs",
        "/run/dittobench-grader:rw,noexec,nosuid,nodev,size=32m,mode=0700",
        "--tmpfs",
        "/run/dittobench-control:rw,noexec,nosuid,nodev,size=1m,mode=0700",
    ]
    if language == "rust":
        args += [
            "--tmpfs",
            "/out:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700",
        ]
    return args


def stable(value):
    return {
        k: v
        for k, v in value.items()
        if k not in ("response_sha256", "supervisor_response")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2, choices=(1, 2))
    args = parser.parse_args()
    os.umask(0o077)
    plan = private_json(args.plan)
    images = private_json(args.images)
    source = output(["git", "rev-parse", "HEAD"], cwd=args.checkout)
    require(
        re.fullmatch(r"[0-9a-f]{40}", source)
        and not output(["git", "status", "--porcelain"], cwd=args.checkout),
        "matrix requires a clean exact source checkout",
    )
    require(
        plan.get("schema") == "dittobench-private-compatibility-plan-v1"
        and plan.get("source_sha") == source
        and plan.get("replicates") == 2,
        "matrix plan/source/repeats rejected",
    )
    cases = plan["cases"]
    require(
        0 < len(cases) <= 512 and set(images) == set(PROFILES), "matrix is incomplete"
    )
    coverage = {}
    for case in cases:
        key = (case["language"], case["group_id"])
        require(
            case["language"] in PROFILES
            and re.fullmatch(r"private-group-[0-9]{3}", case["group_id"]),
            "matrix group rejected",
        )
        pair = (case["role"], case["phase"])
        coverage.setdefault(key, set())
        require(pair not in coverage[key], "duplicate matrix case")
        coverage[key].add(pair)
    require(
        {key[0] for key in coverage} == set(PROFILES)
        and all(
            value
            == {
                ("base", "visible"),
                ("base", "hidden"),
                ("reference", "visible"),
                ("reference", "hidden"),
            }
            for value in coverage.values()
        ),
        "matrix role/phase coverage incomplete",
    )
    for path in (args.corpus, args.helper, args.output.parent):
        require(
            path.is_absolute()
            and path.resolve() == path
            and not any(c in str(path) for c in ",\n\r\0"),
            "mount path rejected",
        )
    require(
        args.corpus.is_dir() and not args.corpus.stat().st_mode & 0o077,
        "corpus root must be private",
    )
    require(
        args.helper.is_file() and not args.helper.stat().st_mode & 0o022,
        "operator helper must be protected",
    )
    resolved = {}
    inspected = {}
    for language, reference in images.items():
        require(
            isinstance(reference, str) and not reference.startswith("-"),
            "image reference rejected",
        )
        value = json.loads(output(["docker", "image", "inspect", reference]))[0]
        resolved[language] = image_policy(value, language, source)
        inspected[language] = {
            "id": value["Id"],
            "descriptor": value.get("Descriptor"),
            "repo_digests": value.get("RepoDigests", []),
        }
    args.output.mkdir(mode=0o700)
    helper_sha = digest(args.helper.read_bytes())
    save(
        args.output,
        "provenance.json",
        {
            "source_sha": source,
            "plan_sha256": digest(args.plan.read_bytes()),
            "helper_sha256": helper_sha,
            "runner_sha256": digest(Path(__file__).read_bytes()),
            "images": inspected,
            "kernel": output(["uname", "-r"]),
            "runtime_qualification": False,
            "production_api_approval": False,
            "image_binding_kind": "local_config_id_not_native_import_approval",
        },
    )

    def run(item):
        index, replicate = item
        case = dict(cases[index])
        language = case["language"]
        require(language in PROFILES, "unknown language")
        case["image_sha256"] = resolved[language][7:]
        name = "coding-private-control-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="case-", dir=args.output) as temp:
            folder = Path(temp)
            save(folder, "case.json", case)
            command = [
                "docker",
                "run",
                "--name",
                name,
                "--rm",
                *envelope(language),
                "--mount",
                f"type=bind,src={args.corpus},dst=/private-input,readonly",
                "--mount",
                f"type=bind,src={folder},dst=/private-control,readonly",
                "--mount",
                f"type=bind,src={args.helper},dst=/operator/control,readonly",
                "--entrypoint",
                "/operator/control",
                resolved[language],
            ]
            try:
                result = subprocess.run(command, capture_output=True, timeout=210)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    timeout=20,
                    check=True,
                )
                raise RuntimeError(
                    "private control timed out; container removed"
                ) from None
            inspection = subprocess.run(
                ["docker", "container", "inspect", name],
                capture_output=True,
                timeout=20,
            )
            require(
                inspection.returncode != 0 and b"No such" in inspection.stderr,
                "private container removal is unconfirmed",
            )
            require(
                len(result.stdout) <= 65536 and len(result.stderr) <= 4096,
                "private control output exceeded bound",
            )
            record = {
                "index": index,
                "replicate": replicate,
                "exit_code": result.returncode,
            }
            if result.stdout:
                record["observation"] = json.loads(result.stdout)
            save(args.output, f"case-{index:04}-{replicate}.json", record)
            require(
                result.returncode == 0
                and not result.stderr
                and record.get("observation", {}).get("expectation_matched") is True,
                "private control failed; private receipt retained",
            )
            return record

    tasks = [(index, replicate) for index in range(len(cases)) for replicate in (1, 2)]
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        iterator = iter(tasks)
        pending = {
            pool.submit(run, next(iterator)) for _ in range(min(args.jobs, len(tasks)))
        }
        while pending:
            done, pending = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                records.append(future.result())
                if len(records) % 20 == 0:
                    print(
                        f"Private controls completed: {len(records)}/{len(tasks)}",
                        flush=True,
                    )
                task = next(iterator, None)
                if task is not None:
                    pending.add(pool.submit(run, task))
    by_case = {}
    for record in records:
        value = stable(record["observation"])
        require(
            record["index"] not in by_case or by_case[record["index"]] == value,
            "private control results changed across repeats",
        )
        by_case[record["index"]] = value
    require(
        digest(args.helper.read_bytes()) == helper_sha
        and output(["git", "rev-parse", "HEAD"], cwd=args.checkout) == source
        and not output(["git", "status", "--porcelain"], cwd=args.checkout),
        "operator or checkout changed during controls",
    )
    save(
        args.output,
        "summary.json",
        {
            "schema": "dittobench-private-compatibility-summary-v1",
            "source_sha": source,
            "controls": len(records),
            "cases": len(cases),
            "replicates": 2,
            "languages": sorted({c["language"] for c in cases}),
            "groups_by_language": {
                language: sum(key[0] == language for key in coverage)
                for language in PROFILES
            },
            "repeat_results_equal": True,
            "private_controls_passed": True,
            "runtime_qualification": False,
            "production_api_approval": False,
            "native_host_ready": False,
            "canary_completed": False,
            "weight_eligible": False,
        },
    )
    print(
        f"Private matrix passed: {len(records)} controls; "
        "native qualification remains separate."
    )


if __name__ == "__main__":
    main()
