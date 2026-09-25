"""Bounded trusted producer runner. Never started by API boot.

This module prepares bytes, not qualification or leases. The approved native
producer runs outside database transactions with only its provider credential.
Deploy with a dedicated UID, private local storage and external disk/egress
limits; neither the executable nor work root may be writable by miners.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import signal
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.queries.private_benchmark_datasets import (
    MAX_ARTIFACT_BYTES,
    MAX_RECEIPT_BYTES,
    PrivateDatasetError,
)
from ditto.db.queries.private_benchmark_preparations import (
    PreparationClaim,
    claim_private_preparation,
    fail_private_preparation,
    finish_private_preparation,
)


class PrivateWorkerError(ValueError):
    """Sanitized operational failure; never contains provider or artifact text."""


@dataclass(frozen=True)
class ProducerConfig:
    executable: Path
    executable_sha256: str
    profile_sha256: str
    work_root: Path
    rewrite_model: str
    rewrite_provider: str
    validator_model: str
    validator_provider: str
    api_key: str = field(repr=False)
    rewrite_reasoning: str = ""
    validator_reasoning: str = ""
    concurrency: int = 4
    max_cost_usd: float = 10
    timeout_seconds: float = 7200

    def arguments(self) -> list[str]:
        return [
            "-rewrite-model",
            self.rewrite_model,
            "-rewrite-provider",
            self.rewrite_provider,
            "-validator-model",
            self.validator_model,
            "-validator-provider",
            self.validator_provider,
            "-rewrite-reasoning",
            self.rewrite_reasoning,
            "-validator-reasoning",
            self.validator_reasoning,
            "-concurrency",
            str(self.concurrency),
            "-max-cost-usd",
            str(self.max_cost_usd),
        ]


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.resolve() != path
    ):
        raise PrivateWorkerError("private worker directory is not protected")


def _read_private(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_nlink != 1
            or not 0 < info.st_size <= maximum
        ):
            raise PrivateWorkerError("private worker output is not protected")
        result = source.read(maximum + 1)
        if len(result) != info.st_size:
            raise PrivateWorkerError("private worker output changed")
        return result


def _check_config(config: ProducerConfig) -> None:
    try:
        if (
            not re.fullmatch(r"[0-9a-f]{64}", config.executable_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", config.profile_sha256)
            or not 1 <= config.concurrency <= 16
            or not math.isfinite(config.max_cost_usd)
            or not 0 < config.max_cost_usd <= 1000
            or not 0 < config.timeout_seconds <= 7200
            or not config.api_key.strip()
            or any(c in config.api_key for c in "\r\n\x00")
        ):
            raise PrivateWorkerError("private worker configuration invalid")
        _private_directory(config.work_root)
        info = config.executable.lstat()
        if (
            not config.executable.is_absolute()
            or config.executable.resolve() != config.executable
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
            or not info.st_mode & 0o111
            or info.st_size > 128 << 20
        ):
            raise PrivateWorkerError("private worker executable is not approved")
        with config.executable.open("rb") as binary:
            digest = hashlib.file_digest(binary, "sha256").hexdigest()
        if digest != config.executable_sha256:
            raise PrivateWorkerError("private worker executable digest mismatch")
    except OSError:
        raise PrivateWorkerError("private worker configuration unavailable") from None


async def _run(
    config: ProducerConfig, arguments: list[str], *, cwd: Path, profile: bool = False
) -> bytes:
    # No inherited environment: specifically no DB, admin, gcloud, proxy or
    # application credentials. Profile inspection gets no provider key either.
    environment = {"LANG": "C.UTF-8", "TMPDIR": str(cwd)}
    if not profile:
        environment["OPENROUTER_API_KEY"] = config.api_key
    process = await asyncio.create_subprocess_exec(
        str(config.executable),
        *config.arguments(),
        *arguments,
        cwd=cwd,
        env=environment,
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if profile else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15 if profile else config.timeout_seconds):
            output = b""
            if profile:
                assert process.stdout is not None
                output = await process.stdout.read(66)
                if len(output) > 65:
                    raise PrivateWorkerError("private worker profile output invalid")
            if await process.wait() != 0:
                raise PrivateWorkerError("private worker producer rejected")
            return output
    finally:
        # Also reap descendants after a normal parent exit. This process group
        # belongs only to the producer launched above; never target a host PID.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def verify_producer(config: ProducerConfig) -> None:
    """Verify approval and profile before claiming or spending inference quota."""
    _check_config(config)
    try:
        output = await _run(
            config, ["-profile-sha"], cwd=config.work_root, profile=True
        )
    except (OSError, TimeoutError):
        raise PrivateWorkerError("private worker profile unavailable") from None
    if output != (config.profile_sha256 + "\n").encode():
        raise PrivateWorkerError("private worker profile digest mismatch")


async def _produce(config: ProducerConfig, claim: PreparationClaim) -> dict[str, bytes]:
    directory = config.work_root / f"{claim.preparation_id}-{claim.token}"
    directory.mkdir(mode=0o700)
    _private_directory(directory)
    salt = directory / "salt.bin"
    fd = os.open(salt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as destination:
        destination.write(claim.surface_salt)
        destination.flush()
        os.fsync(destination.fileno())
    output = directory / "output"
    await _run(
        config,
        [
            "-seed",
            str(claim.identity.seed),
            "-run-size",
            claim.identity.run_size,
            "-salt-file",
            str(salt),
            "-output",
            str(output),
        ],
        cwd=directory,
    )
    _private_directory(output)
    return {
        "base_bytes": _read_private(output / "base.json", MAX_ARTIFACT_BYTES),
        "dataset_bytes": _read_private(output / "dataset.json", MAX_ARTIFACT_BYTES),
        "validation_receipt_bytes": _read_private(
            output / "validation.json", MAX_RECEIPT_BYTES
        ),
    }


async def run_once(
    config: ProducerConfig, sessions: async_sessionmaker[AsyncSession]
) -> str:
    """Prepare at most one artifact. Returns idle/ready/failed/stale, never bytes.

    A killed worker leaves a recoverable claim. A known producer failure is
    terminal, not an automatic paid retry. Files remain private for diagnosis.
    """
    await verify_producer(config)
    async with sessions() as session, session.begin():
        claim = await claim_private_preparation(
            session,
            transform_profile_sha256=config.profile_sha256,
            now=datetime.now(UTC),
        )
    if claim is None:
        return "idle"
    code = "producer_rejected"
    try:
        values = await _produce(config, claim)
        code = "invalid_output"
        async with sessions() as session, session.begin():
            await finish_private_preparation(
                session, claim=claim, now=datetime.now(UTC), **values
            )
        return "ready"
    except (PrivateWorkerError, PrivateDatasetError, OSError, TimeoutError):
        try:
            async with sessions() as session, session.begin():
                await fail_private_preparation(
                    session, claim=claim, now=datetime.now(UTC), code=code
                )
        except PrivateDatasetError:
            return "stale"
        return "failed"


def config_from_env() -> ProducerConfig:
    """Separate worker configuration, not part of the public API environment."""
    prefix = "DITTO_PRIVATE_PRODUCER_"
    try:
        return ProducerConfig(
            executable=Path(os.environ[prefix + "EXECUTABLE"]),
            executable_sha256=os.environ[prefix + "EXECUTABLE_SHA256"],
            profile_sha256=os.environ[prefix + "PROFILE_SHA256"],
            work_root=Path(os.environ[prefix + "WORK_ROOT"]),
            rewrite_model=os.environ[prefix + "REWRITE_MODEL"],
            rewrite_provider=os.environ[prefix + "REWRITE_PROVIDER"],
            validator_model=os.environ[prefix + "VALIDATOR_MODEL"],
            validator_provider=os.environ[prefix + "VALIDATOR_PROVIDER"],
            api_key=os.environ["OPENROUTER_API_KEY"],
            rewrite_reasoning=os.environ.get(prefix + "REWRITE_REASONING", ""),
            validator_reasoning=os.environ.get(prefix + "VALIDATOR_REASONING", ""),
            concurrency=int(os.environ.get(prefix + "CONCURRENCY", "4")),
            max_cost_usd=float(os.environ[prefix + "MAX_COST_USD"]),
        )
    except (KeyError, ValueError):
        raise PrivateWorkerError("private worker environment incomplete") from None


async def _main() -> int:
    from ditto.db.factory import create_db_engine, create_session_maker

    config = config_from_env()
    engine = create_db_engine()
    try:
        result = await run_once(config, create_session_maker(engine))
        print(result)
        return 0 if result in {"ready", "idle"} else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        status = asyncio.run(_main())
    except Exception:
        # Database errors can carry failing-row detail even with hidden binds.
        # Private diagnostics stay in the owner-only work directory, not logs.
        print("private worker unavailable", file=sys.stderr)
        status = 1
    raise SystemExit(status)
