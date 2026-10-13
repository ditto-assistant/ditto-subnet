"""Trusted Season 2 reranker scorer worker (SN-209). Shadow only, off by default.

Never started by API boot. ``python -m ditto.reranker_scoring_worker`` scores
at most one submission and exits (``--loop`` keeps polling). It does nothing
unless ``DITTO_RERANKER_SCORING_ENABLED`` is true.

One run:

1. Verify the configuration before touching the queue: the pool generator
   binary must match its pinned SHA-256, the work root must be owner-only, and
   the incumbent artifact must match ``CONTRACT.incumbent`` byte for byte.
2. Lease the oldest ``accepted`` submission (``accepted -> scoring`` with an
   opaque token, committed before any slow work). A crashed worker's lease
   expires and is reclaimed, at most ``max_attempts`` times.
3. Derive the pool seed from a **finalized** block hash read now, after the
   submission committed (:func:`derive_reranker_seed`), and pin it. A retry
   reuses the pinned seed, so the Platform cannot re-roll it.
4. Generate pools with the pinned ``rerank-pools`` binary under the held
   surface salt. The salt reaches the child only through an owner-only file:
   never argv, the environment, logs, the score, or any API.
5. Score the challenger and the current shadow champion (the incumbent when
   nobody holds the crown) on the same pools, compare them with the paired
   dethrone test, and time the challenger against the incumbent side by side.
6. Write the score and, in shadow, crown the challenger only when it dethrones,
   passes latency, and passes the transfer gate (SN-226). The transfer gate is
   not implemented yet, so with the default ``require_transfer_gate=True``
   nobody is crowned until SN-226 lands.

Nothing here touches weights, emissions, payments or the memory track.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from dittobench_reranker.artifact import Artifact, ArtifactError, load_artifact
from dittobench_reranker.contract import CONTRACT, CONTRACT_ID
from dittobench_reranker.pools import PoolBundle, PoolFormatError, parse_bundle

from ditto.api_server.onchain_seed import derive_reranker_seed, normalize_block_hash
from ditto.db.queries.reranker import (
    RerankerChampionChangedError,
    RerankerScoringClaim,
    StaleRerankerClaimError,
    claim_reranker_submission,
    crown_reranker_champion,
    finish_reranker_submission,
    get_current_reranker_champion,
    get_reranker_submission,
    pin_reranker_seed,
    release_reranker_claim,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.chain.models import BlockInfo

logger = logging.getLogger(__name__)

SCORE_SCHEMA = "reranker-score-v1"
ENV_PREFIX = "DITTO_RERANKER_"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# Mirrors the intake cap (contract file caps plus 1 MiB of tar overhead).
_ARTIFACT_MAX_BYTES = (
    CONTRACT.artifact.max_model_bytes
    + CONTRACT.artifact.max_vocab_bytes
    + CONTRACT.artifact.max_manifest_bytes
    + 1024 * 1024
)
_MAX_REASON_CHARS = 500


class ScorerConfigError(ValueError):
    """The worker is misconfigured; nothing was claimed. Never holds secrets."""


class _Transient(Exception):  # noqa: N818 - internal control flow
    """Infrastructure trouble: return the claim for a later attempt."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Terminal(Exception):  # noqa: N818 - internal control flow
    """A deterministic failure: retrying would fail the same way."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Invalid(Exception):  # noqa: N818 - internal control flow
    """The artifact itself breaks the contract (miner-facing reason)."""


@dataclass(frozen=True)
class ScorerConfig:
    poolgen_executable: Path
    poolgen_sha256: str
    # The held surface salt. Secret: kept out of repr, argv, logs and scores.
    surface_salt: int = field(repr=False)
    work_root: Path
    incumbent_path: Path
    pool_seed_count: int = 4
    run_size: str = "full"
    bench_version: int = 13
    require_transfer_gate: bool = True
    lease_seconds: int = 3600
    max_attempts: int = 3
    poolgen_timeout_seconds: float = 1800.0
    max_pools_bytes: int = 512 * 1024 * 1024
    # Production always pins the contract's incumbent; only tests override.
    incumbent_model_sha256: str = CONTRACT.incumbent.model_sha256
    incumbent_vocab_sha256: str = CONTRACT.incumbent.vocab_sha256

    def policy(self) -> dict[str, Any]:
        """The non-secret settings recorded in every score (Backroom-visible)."""
        return {
            "poolgen_sha256": self.poolgen_sha256,
            "pool_seed_count": self.pool_seed_count,
            "run_size": self.run_size,
            "bench_version": self.bench_version,
            "require_transfer_gate": self.require_transfer_gate,
        }


class FinalizedBlockReader(Protocol):
    async def get_finalized_block(self) -> BlockInfo: ...


class ArtifactStore(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes: ...


def artifact_key(submission_id: UUID) -> str:
    """Object key the intake endpoint stores an accepted artifact under."""
    return f"reranker/{submission_id}/artifact.tar.gz"


# --- Configuration checks -------------------------------------------------


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.resolve() != path
    ):
        raise ScorerConfigError("reranker scorer work root is not owner-only")


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def check_config(config: ScorerConfig) -> None:
    """Refuse to run (before claiming anything) on an unapproved setup."""
    if (
        not re.fullmatch(r"[0-9a-f]{64}", config.poolgen_sha256)
        or not 0 < config.surface_salt < 1 << 64
        or not 1 <= config.pool_seed_count <= 64
        or config.run_size not in {"small", "medium", "full"}
        or not 13 <= config.bench_version <= 1000
        or not 60 <= config.lease_seconds <= 86_400
        or not 1 <= config.max_attempts <= 10
        or not 0 < config.poolgen_timeout_seconds <= 7200
        or not 0 < config.max_pools_bytes <= 4 << 30
    ):
        raise ScorerConfigError("reranker scorer configuration invalid")
    try:
        _private_directory(config.work_root)
        executable = config.poolgen_executable
        info = executable.lstat()
        if (
            not executable.is_absolute()
            or executable.resolve() != executable
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
            or not info.st_mode & 0o111
            or info.st_size > 256 << 20
        ):
            raise ScorerConfigError("pool generator executable is not approved")
        if _sha256_file(executable) != config.poolgen_sha256:
            raise ScorerConfigError("pool generator digest mismatch")
    except OSError:
        raise ScorerConfigError("reranker scorer configuration unavailable") from None


def load_incumbent(config: ScorerConfig) -> Artifact:
    """Load the incumbent and prove it is the pinned product model."""
    try:
        incumbent = load_artifact(config.incumbent_path)
    except (ArtifactError, OSError):
        raise ScorerConfigError("incumbent artifact unavailable") from None
    if (
        hashlib.sha256(incumbent.model).hexdigest() != config.incumbent_model_sha256
        or hashlib.sha256(incumbent.vocab).hexdigest() != config.incumbent_vocab_sha256
    ):
        raise ScorerConfigError("incumbent artifact does not match the contract")
    return incumbent


# --- Pool generation ------------------------------------------------------


def _read_private(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or not 0 < info.st_size <= maximum
        ):
            raise _Terminal("pools_invalid")
        data = source.read(maximum + 1)
        if len(data) != info.st_size:
            raise _Terminal("pools_invalid")
        return data


async def generate_pools(config: ScorerConfig, seed: int, directory: Path) -> bytes:
    """Run the pinned generator; the salt reaches it only via a 0600 file."""
    salt_file = directory / "salt"
    fd = os.open(salt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(str(config.surface_salt))
    out = directory / "pools.json"
    argv = [
        str(config.poolgen_executable),
        "-seed",
        str(seed),
        "-count",
        str(config.pool_seed_count),
        "-bench-version",
        str(config.bench_version),
        "-run-size",
        config.run_size,
        "-surface-salt-file",
        str(salt_file),
        "-out",
        str(out),
    ]
    # No inherited environment: no database, storage, chain or admin secrets.
    environment = {"LANG": "C.UTF-8", "TMPDIR": str(directory)}
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=directory,
            env=environment,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise _Transient("pool_generation_failed") from None
    try:
        async with asyncio.timeout(config.poolgen_timeout_seconds):
            code = await process.wait()
    except TimeoutError:
        raise _Transient("pool_generation_failed") from None
    finally:
        # Reap the generator's whole process group, never a host PID.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        with suppress(FileNotFoundError):
            salt_file.unlink()
    if code != 0:
        raise _Transient("pool_generation_failed")
    try:
        return _read_private(out, config.max_pools_bytes)
    except OSError:
        raise _Transient("pool_generation_failed") from None


def parse_pools(config: ScorerConfig, raw: bytes, seed: int) -> PoolBundle:
    try:
        bundle = parse_bundle(raw, CONTRACT.scoring.pool_size)
    except PoolFormatError:
        raise _Terminal("pools_invalid") from None
    expected = tuple(seed + i for i in range(config.pool_seed_count))
    if bundle.seeds != expected or bundle.bench_version != config.bench_version:
        raise _Terminal("pools_invalid")
    if len(bundle.pools) < 2:
        raise _Terminal("pools_invalid")
    return bundle


# --- Artifacts ------------------------------------------------------------


def _short_reason(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:_MAX_REASON_CHARS] or type(exc).__name__


async def _fetch_artifact(
    store: ArtifactStore,
    submission_id: UUID,
    expected_sha256: str,
    directory: Path,
    *,
    unavailable: str,
    challenger: bool,
) -> Artifact:
    from ditto.api_server.endpoints.reranker_upload import _validate_archive

    try:
        body = await store.get_object(
            key=artifact_key(submission_id), max_bytes=_ARTIFACT_MAX_BYTES
        )
    except Exception:
        raise _Transient(unavailable) from None
    path = directory / f"{submission_id}.tar.gz"
    path.write_bytes(body)
    try:
        # The intake's own bounded prescan + static validation.
        artifact = await asyncio.to_thread(_validate_archive, path)
    except ArtifactError as exc:
        if challenger:
            raise _Invalid(f"artifact rejected: {_short_reason(exc)}") from None
        raise _Terminal(unavailable) from None
    finally:
        path.unlink(missing_ok=True)
    if artifact.sha256 != expected_sha256:
        raise _Terminal("artifact_mismatch" if challenger else unavailable)
    return artifact


# --- Scoring --------------------------------------------------------------


def _summary(report: Any) -> dict[str, Any]:
    data = report.to_dict()
    data.pop("per_pool", None)
    return data


@dataclass(frozen=True)
class _Champion:
    kind: str  # "incumbent" | "submission"
    submission_id: UUID | None
    miner_hotkey: str | None
    artifact: Artifact = field(repr=False)


def _score_both(
    challenger: Artifact,
    champion: Artifact,
    incumbent: Artifact,
    bundle: PoolBundle,
) -> tuple[Any, Any, Any, Any]:
    """CPU-bound; one intra-op thread to score, four to time (contract)."""
    from dittobench_reranker.scorer import (
        ScoringError,
        compare,
        measure_latency,
        score,
    )

    try:
        challenger_report = score(challenger, bundle)
    except ScoringError as exc:
        raise _Invalid(f"model output rejected: {_short_reason(exc)}") from None
    except Exception:
        raise _Invalid("model could not be executed by the scorer") from None
    try:
        champion_report = score(champion, bundle)
    except Exception:
        raise _Terminal("champion_unavailable") from None
    try:
        comparison = compare(champion_report, challenger_report)
        latency = measure_latency(challenger, bundle, incumbent=incumbent)
    except Exception:
        raise _Terminal("scoring_error") from None
    return challenger_report, champion_report, comparison, latency


def crown_decision(
    *, dethrones: bool, latency_passed: bool, transfer_gate: str, require: bool
) -> tuple[bool, list[str]]:
    """Whether a challenger takes the shadow crown, and what blocked it."""
    blocked: list[str] = []
    if not dethrones:
        blocked.append("no_dethrone")
    if not latency_passed:
        blocked.append("latency_gate")
    if require and transfer_gate != "passed":
        blocked.append("transfer_gate_" + transfer_gate)
    return not blocked, blocked


# --- One run --------------------------------------------------------------


Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _release_or_fail(
    sessions: async_sessionmaker[AsyncSession],
    claim: RerankerScoringClaim,
    code: str,
    *,
    max_attempts: int,
    now: datetime,
) -> str:
    """Requeue after infrastructure trouble; fail once attempts run out."""
    try:
        async with sessions() as session, session.begin():
            if claim.attempts < max_attempts:
                await release_reranker_claim(session, claim=claim)
                return "requeued"
            await finish_reranker_submission(
                session, claim=claim, now=now, status="failed", failure_reason=code
            )
            return "failed"
    except StaleRerankerClaimError:
        return "stale"


async def run_once(
    config: ScorerConfig,
    sessions: async_sessionmaker[AsyncSession],
    chain: FinalizedBlockReader,
    store: ArtifactStore,
    *,
    clock: Clock = _utcnow,
) -> str:
    """Score at most one submission.

    Returns ``idle``, ``scored``, ``invalid``, ``failed``, ``requeued`` or
    ``stale`` (the lease was taken over). Raises :class:`ScorerConfigError`
    before claiming anything when the setup is not approved.
    """
    check_config(config)
    incumbent = load_incumbent(config)
    async with sessions() as session, session.begin():
        claim = await claim_reranker_submission(
            session,
            now=clock(),
            lease=timedelta(seconds=config.lease_seconds),
            max_attempts=config.max_attempts,
        )
    if claim is None:
        return "idle"
    directory = config.work_root / f"{claim.submission_id}-{claim.token}"
    try:
        directory.mkdir(mode=0o700)
        _private_directory(directory)
        outcome = await _score_claim(
            config, sessions, chain, store, claim, incumbent, directory, clock
        )
    except StaleRerankerClaimError:
        outcome = "stale"
    except _Transient as exc:
        outcome = await _release_or_fail(
            sessions,
            claim,
            exc.code,
            max_attempts=config.max_attempts,
            now=clock(),
        )
    except _Terminal as exc:
        outcome = await _finish_failed(sessions, claim, exc.code, clock())
    except _Invalid as exc:
        outcome = await _finish_invalid(sessions, claim, str(exc), None, clock())
    except Exception as exc:
        # Type only: exception text can carry paths or provider detail.
        logger.error(
            "reranker scoring crashed submission_id=%s error=%s",
            claim.submission_id,
            type(exc).__name__,
        )
        outcome = await _finish_failed(sessions, claim, "internal_error", clock())
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    logger.info(
        "reranker scoring submission_id=%s attempt=%d outcome=%s",
        claim.submission_id,
        claim.attempts,
        outcome,
    )
    return outcome


async def _finish_failed(
    sessions: async_sessionmaker[AsyncSession],
    claim: RerankerScoringClaim,
    code: str,
    now: datetime,
) -> str:
    try:
        async with sessions() as session, session.begin():
            await finish_reranker_submission(
                session, claim=claim, now=now, status="failed", failure_reason=code
            )
    except StaleRerankerClaimError:
        return "stale"
    return "failed"


async def _finish_invalid(
    sessions: async_sessionmaker[AsyncSession],
    claim: RerankerScoringClaim,
    reason: str,
    score: dict[str, Any] | None,
    now: datetime,
) -> str:
    try:
        async with sessions() as session, session.begin():
            await finish_reranker_submission(
                session,
                claim=claim,
                now=now,
                status="invalid",
                invalid_reason=reason,
                score=score,
            )
    except StaleRerankerClaimError:
        return "stale"
    return "invalid"


async def _pin_seed(
    sessions: async_sessionmaker[AsyncSession],
    chain: FinalizedBlockReader,
    claim: RerankerScoringClaim,
) -> tuple[int, int, str]:
    if (
        claim.seed is not None
        and claim.seed_block is not None
        and claim.seed_block_hash is not None
    ):
        return claim.seed, claim.seed_block, claim.seed_block_hash
    try:
        block = await chain.get_finalized_block()
    except Exception:
        raise _Transient("chain_unavailable") from None
    block_hash = normalize_block_hash(block.hash)
    if not re.fullmatch(r"[0-9a-f]{64}", block_hash) or block.number < 0:
        raise _Transient("chain_unavailable")
    seed = derive_reranker_seed(
        block_hash, claim.submission_id, contract_id=CONTRACT_ID
    )
    async with sessions() as session, session.begin():
        await pin_reranker_seed(
            session,
            claim=claim,
            seed=seed,
            seed_block=block.number,
            seed_block_hash=block_hash,
        )
    return seed, block.number, block_hash


async def _current_champion(
    sessions: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    incumbent: Artifact,
    directory: Path,
) -> _Champion:
    async with sessions() as session:
        current = await get_current_reranker_champion(session)
        row = (
            await get_reranker_submission(session, submission_id=current.submission_id)
            if current is not None
            else None
        )
    if current is None:
        return _Champion("incumbent", None, None, incumbent)
    if row is None:
        raise _Terminal("champion_unavailable")
    artifact = await _fetch_artifact(
        store,
        row.id,
        row.artifact_sha256,
        directory,
        unavailable="champion_unavailable",
        challenger=False,
    )
    return _Champion("submission", row.id, row.miner_hotkey, artifact)


async def _score_claim(
    config: ScorerConfig,
    sessions: async_sessionmaker[AsyncSession],
    chain: FinalizedBlockReader,
    store: ArtifactStore,
    claim: RerankerScoringClaim,
    incumbent: Artifact,
    directory: Path,
    clock: Clock,
) -> str:
    seed, seed_block, seed_block_hash = await _pin_seed(sessions, chain, claim)
    challenger = await _fetch_artifact(
        store,
        claim.submission_id,
        claim.artifact_sha256,
        directory,
        unavailable="artifact_unavailable",
        challenger=True,
    )
    champion = await _current_champion(sessions, store, incumbent, directory)
    if champion.submission_id == claim.submission_id:
        # Cannot happen through the queue (a champion was already scored).
        raise _Terminal("internal_error")
    bundle = parse_pools(config, await generate_pools(config, seed, directory), seed)
    challenger_report, champion_report, comparison, latency = await asyncio.to_thread(
        _score_both, challenger, champion.artifact, incumbent, bundle
    )
    transfer_gate = "not_evaluated"  # SN-226 fills this in.
    crowned, blocked = crown_decision(
        dethrones=comparison.dethrones,
        latency_passed=latency.passed,
        transfer_gate=transfer_gate,
        require=config.require_transfer_gate,
    )
    comparison_doc = asdict(comparison)
    score: dict[str, Any] = {
        "schema": SCORE_SCHEMA,
        "shadow": True,
        "contract_id": CONTRACT_ID,
        "contract_sha256": challenger_report.contract_sha256,
        "pools_sha256": bundle.sha256,
        "pools": len(bundle.pools),
        # Decimal strings: JSON numbers lose precision above 2**53.
        "seed": str(seed),
        "seed_block": seed_block,
        "seed_block_hash": seed_block_hash,
        "policy": config.policy(),
        "challenger": _summary(challenger_report),
        "champion": {
            "kind": champion.kind,
            "submission_id": (
                str(champion.submission_id) if champion.submission_id else None
            ),
            "miner_hotkey": champion.miner_hotkey,
            **_summary(champion_report),
        },
        "comparison": comparison_doc,
        "latency": asdict(latency),
        "transfer_gate": {
            "status": transfer_gate,
            "required": config.require_transfer_gate,
        },
        # Would dethrone on score and latency alone (ignores the transfer gate).
        "dethrones_shadow": bool(comparison.dethrones and latency.passed),
        "crowned": False,
        "crown_blocked_by": blocked,
    }
    now = clock()
    if not latency.passed:
        ratio = latency.ratio_to_incumbent or 0.0
        reason = (
            f"latency gate: p50 is {ratio:.2f}x the incumbent "
            f"(limit {latency.max_ratio:g}x)"
        )
        return await _finish_invalid(sessions, claim, reason, score, now)
    try:
        async with sessions() as session, session.begin():
            if crowned:
                await crown_reranker_champion(
                    session,
                    submission_id=claim.submission_id,
                    expected_previous=champion.submission_id,
                    contract_sha256=challenger_report.contract_sha256,
                    pools_sha256=bundle.sha256,
                    comparison=comparison_doc,
                    now=now,
                )
                score["crowned"] = True
            await finish_reranker_submission(
                session, claim=claim, now=now, status="scored", score=score
            )
    except RerankerChampionChangedError:
        # Another worker moved the crown while this one scored: the challenger
        # must beat the new champion, on its same pinned seed.
        return await _release_or_fail(
            sessions,
            claim,
            "champion_unavailable",
            max_attempts=config.max_attempts,
            now=clock(),
        )
    return "scored"


# --- Entry point ----------------------------------------------------------


def scoring_enabled() -> bool:
    return os.environ.get(ENV_PREFIX + "SCORING_ENABLED", "false").lower() in _TRUTHY


def config_from_env() -> ScorerConfig:
    """Worker-only environment; never part of the public API process."""
    env = os.environ
    p = ENV_PREFIX
    try:
        return ScorerConfig(
            poolgen_executable=Path(env[p + "POOLGEN_EXECUTABLE"]),
            poolgen_sha256=env[p + "POOLGEN_SHA256"].strip().lower(),
            surface_salt=int(env[p + "SURFACE_SALT"]),
            work_root=Path(env[p + "WORK_ROOT"]),
            incumbent_path=Path(env[p + "INCUMBENT_PATH"]),
            pool_seed_count=int(env.get(p + "POOL_SEED_COUNT", "4")),
            run_size=env.get(p + "POOL_RUN_SIZE", "full"),
            bench_version=int(env.get(p + "POOL_BENCH_VERSION", "13")),
            require_transfer_gate=env.get(p + "REQUIRE_TRANSFER_GATE", "true").lower()
            in _TRUTHY,
            lease_seconds=int(env.get(p + "SCORING_LEASE_SECONDS", "3600")),
            max_attempts=int(env.get(p + "SCORING_MAX_ATTEMPTS", "3")),
            poolgen_timeout_seconds=float(
                env.get(p + "POOLGEN_TIMEOUT_SECONDS", "1800")
            ),
        )
    except (KeyError, ValueError):
        # Never echo the values: one of them is the held salt.
        raise ScorerConfigError("reranker scorer environment incomplete") from None


async def _main(loop: bool, poll_seconds: float) -> int:
    from ditto.api_server.storage.factory import create_storage_client
    from ditto.chain.factory import create_chain_client
    from ditto.chain.models import parse_chain_config_from_env
    from ditto.db.factory import create_db_engine, create_session_maker

    if not scoring_enabled():
        print("disabled")
        return 0
    config = config_from_env()
    check_config(config)
    engine = create_db_engine()
    try:
        sessions = create_session_maker(engine)
        async with (
            create_chain_client(parse_chain_config_from_env()) as chain,
            create_storage_client() as store,
        ):
            while True:
                outcome = await run_once(config, sessions, chain, store)
                print(outcome, flush=True)
                if not loop:
                    return 0 if outcome in {"idle", "scored", "invalid"} else 1
                if outcome == "idle":
                    await asyncio.sleep(poll_seconds)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ditto.reranker_scoring_worker")
    parser.add_argument(
        "--loop", action="store_true", help="keep polling instead of exiting"
    )
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        return asyncio.run(_main(args.loop, max(args.poll_seconds, 1.0)))
    except ScorerConfigError as exc:
        print(f"reranker scorer refused to start: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Database errors can carry row detail; print the type only.
        print(f"reranker scorer unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
