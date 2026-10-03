"""Real subprocess and Postgres checks for the trusted private producer runner."""

import asyncio
import hashlib
import os
import sys
from dataclasses import replace

import pytest

from ditto.db.models import PrivateBenchmarkPreparation
from ditto.db.queries.private_benchmark_datasets import find_private_dataset
from ditto.db.queries.private_benchmark_preparations import request_private_preparation
from ditto.private_benchmark_worker import (
    PrivateWorkerError,
    ProducerConfig,
    _read_private,
    run_once,
    verify_producer,
)
from ditto.tests.db.test_private_benchmark_datasets import identity


def producer(tmp_path, profile, mode="success"):
    root = (tmp_path / "private").resolve()
    root.mkdir(mode=0o700)
    binary = root / "producer"
    binary.write_text(f"""#!{sys.executable}
import hashlib, json, os, pathlib, sys, time
args = sys.argv[1:]
assert "ADMIN_SECRET_SENTINEL" not in os.environ
assert "POSTGRES_PASSWORD" not in os.environ
if "-profile-sha" in args:
    assert "OPENROUTER_API_KEY" not in os.environ
    print({profile!r})
    sys.exit(0)
assert os.environ["OPENROUTER_API_KEY"] == "only-provider-secret"
assert pathlib.Path(os.environ["TMPDIR"]).stat().st_mode & 0o777 == 0o700
mode = {mode!r}
if mode == "sleep":
    time.sleep(60)
if mode == "reject":
    print("PRIVATE RESPONSE AND CREDENTIAL", file=sys.stderr)
    sys.exit(1)
def arg(name): return args[args.index(name) + 1]
assert float(arg("-max-cost-usd")) == 10
salt = int.from_bytes(pathlib.Path(arg("-salt-file")).read_bytes(), "big")
common = {{"seed": int(arg("-seed")), "bench_version": 13, "surface_salt": salt}}
base = json.dumps({{**common, "prompt": "base"}}).encode()
data = json.dumps({{**common, "prompt": "private"}}).encode()
sha = lambda body: hashlib.sha256(body).hexdigest()
receipt = json.dumps({{
    "schema": "private-surface-validation-v1", "accepted": True,
    "base_sha256": sha(base), "dataset_sha256": sha(data),
    "transform_profile_sha256": {profile!r}
}}).encode()
output = pathlib.Path(arg("-output"))
output.mkdir(mode=0o700)
files = [("base.json", base), ("dataset.json", data), ("validation.json", receipt)]
for name, body in files:
    path = output / name
    path.write_bytes(body)
    path.chmod(0o600)
if mode == "public":
    (output / "dataset.json").chmod(0o644)
if mode == "symlink":
    (output / "dataset.json").unlink()
    (output / "dataset.json").symlink_to(output / "base.json")
""")
    binary.chmod(0o700)
    return ProducerConfig(
        executable=binary,
        executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        profile_sha256=profile,
        work_root=root,
        rewrite_model="writer",
        rewrite_provider="route-a",
        validator_model="judge",
        validator_provider="route-b",
        api_key="only-provider-secret",
        timeout_seconds=5,
    )


async def reserve(sessions, key):
    async with sessions() as session, session.begin():
        return await request_private_preparation(session, identity=key)


async def test_worker_pins_once_without_inheriting_credentials(
    tmp_path, session_maker, monkeypatch
):
    key = identity()
    config = producer(tmp_path, key.transform_profile_sha256)
    monkeypatch.setenv("ADMIN_SECRET_SENTINEL", "never-inherit")
    await reserve(session_maker, key)
    assert await run_once(config, session_maker) == "ready"
    assert await run_once(config, session_maker) == "idle"
    async with session_maker() as session:
        result = await find_private_dataset(session, identity=key)
        assert result is not None
    assert "only-provider-secret" not in repr(config)


@pytest.mark.parametrize("mode", ["reject", "sleep", "public", "symlink"])
async def test_worker_failure_is_terminal_and_never_pins(
    tmp_path, session_maker, mode, capfd
):
    key = identity()
    config = producer(tmp_path, key.transform_profile_sha256, mode)
    if mode == "sleep":
        config = replace(config, timeout_seconds=0.1)
    prep = await reserve(session_maker, key)
    assert await run_once(config, session_maker) == "failed"
    assert await run_once(config, session_maker) == "idle"
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=key) is None
        row = await session.get(PrivateBenchmarkPreparation, prep)
        assert row.state == "failed"
    assert "PRIVATE RESPONSE" not in str(capfd.readouterr())


@pytest.mark.parametrize(
    "field,value",
    [
        ("executable_sha256", "b" * 64),
        ("profile_sha256", "b" * 64),
        ("timeout_seconds", 10801),
        ("concurrency", 17),
        ("rewrite_mode", "unapproved-mode"),
        ("max_cost_usd", 0),
        ("max_cost_usd", float("nan")),
        ("max_cost_usd", float("inf")),
    ],
)
async def test_bad_approval_never_claims(tmp_path, session_maker, field, value):
    key = identity()
    config = replace(producer(tmp_path, key.transform_profile_sha256), **{field: value})
    prep = await reserve(session_maker, key)
    with pytest.raises(PrivateWorkerError):
        await run_once(config, session_maker)
    async with session_maker() as session:
        row = await session.get(PrivateBenchmarkPreparation, prep)
        assert row.state == "pending" and row.attempts == 0


async def test_cancellation_retains_recoverable_claim(tmp_path, session_maker):
    key = identity()
    config = producer(tmp_path, key.transform_profile_sha256, "sleep")
    prep = await reserve(session_maker, key)
    task = asyncio.create_task(run_once(config, session_maker))
    for _ in range(100):
        await asyncio.sleep(0.02)
        async with session_maker() as session:
            row = await session.get(PrivateBenchmarkPreparation, prep)
            if row.state == "running":
                break
    else:
        pytest.fail("worker did not claim")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with session_maker() as session:
        row = await session.get(PrivateBenchmarkPreparation, prep)
        assert row.state == "running"
        assert await find_private_dataset(session, identity=key) is None


async def test_world_readable_root_rejected(tmp_path):
    config = producer(tmp_path, "a" * 64)
    config.work_root.chmod(0o755)
    with pytest.raises(PrivateWorkerError):
        await verify_producer(config)


def test_rewrite_mode_is_explicit_and_legacy_arguments_are_unchanged(tmp_path):
    config = producer(tmp_path, "a" * 64)
    assert "-rewrite-mode" not in config.arguments()
    literal = replace(config, rewrite_mode="literal-text-v1")
    assert literal.arguments() == [
        "-rewrite-mode",
        "literal-text-v1",
        *config.arguments(),
    ]


async def test_literal_mode_is_passed_during_profile_inspection(tmp_path):
    config = producer(tmp_path, "a" * 64)
    text = config.executable.read_text().replace(
        "args = sys.argv[1:]",
        'args = sys.argv[1:]\nassert args[:2] == ["-rewrite-mode", "literal-text-v1"]',
    )
    config.executable.write_text(text)
    config = replace(
        config,
        executable_sha256=hashlib.sha256(config.executable.read_bytes()).hexdigest(),
        rewrite_mode="literal-text-v1",
    )
    await verify_producer(config)
    with pytest.raises(PrivateWorkerError, match="profile digest mismatch"):
        await verify_producer(replace(config, profile_sha256="b" * 64))


def test_bounded_read_rejects_hardlinks_and_oversize(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"12345")
    path.chmod(0o600)
    with pytest.raises(PrivateWorkerError):
        _read_private(path, 4)
    os.link(path, tmp_path / "alias")
    with pytest.raises(PrivateWorkerError):
        _read_private(path, 5)
