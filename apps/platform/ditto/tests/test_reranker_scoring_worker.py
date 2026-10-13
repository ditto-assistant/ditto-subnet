"""Trusted reranker scorer worker (SN-209): real Postgres, real ONNX, fake chain.

The pool generator is a stand-in executable that writes the
``dittobench-rerank-pools-v1`` format and records how it was invoked, so the
digest pin, the salt hand-off and the clean environment are exercised through
a real subprocess.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytest.importorskip(
    "onnxruntime", reason="install the reranker-scorer group to run scorer tests"
)

from dittobench_reranker.artifact import load_artifact  # noqa: E402
from dittobench_reranker.contract import CONTRACT_ID  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from ditto.api_server.onchain_seed import (  # noqa: E402
    derive_reranker_seed,
    derive_seed,
)
from ditto.chain.errors import ChainConnectionError  # noqa: E402
from ditto.chain.models import BlockInfo  # noqa: E402
from ditto.db.models import RerankerChampion, RerankerSubmission  # noqa: E402
from ditto.db.queries.reranker import (  # noqa: E402
    StaleRerankerClaimError,
    claim_reranker_submission,
    finish_reranker_submission,
    insert_reranker_submission,
)
from ditto.reranker_scoring_worker import (  # noqa: E402
    ScorerConfig,
    ScorerConfigError,
    artifact_key,
    crown_decision,
    main,
    run_once,
)
from ditto.tests.reranker_artifacts import (  # noqa: E402
    build_model,
    manifest,
    tarball,
    vocab_text,
)

SALT = 987_654_321_987_654_321
BLOCK = BlockInfo(number=4_242_424, hash="0x" + "ab" * 32)
POOLS_PER_SEED = 6


class FakeChain:
    def __init__(self, block: BlockInfo | None = BLOCK) -> None:
        self.block = block
        self.reads = 0

    async def get_finalized_block(self) -> BlockInfo:
        self.reads += 1
        if self.block is None:
            raise ChainConnectionError("node unreachable at 10.9.8.7")
        return self.block


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        body = self.objects[key]  # KeyError: an unavailable object
        assert len(body) <= max_bytes
        return body


def poolgen_script(log: Path, mode: str) -> str:
    return f"""#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
def arg(name): return args[args.index(name) + 1]
salt = pathlib.Path(arg("-surface-salt-file")).read_text()
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{"argv": args, "env": sorted(os.environ),
                         "salt": salt}}) + "\\n")
mode = {mode!r}
if mode == "fail":
    sys.exit(3)
seed, count = int(arg("-seed")), int(arg("-count"))
if mode == "wrong-seed":
    seed += 1
sets = []
for s in range(seed, seed + count):
    pools = []
    for p in range({POOLS_PER_SEED}):
        cands = [{{"id": f"c{{i:02d}}", "text": "hay hay"}} for i in range(20)]
        cands[18]["text"] = "the needle"
        cands[19]["text"] = "needle is here"
        pools.append({{"pool_id": f"s{{s}}p{{p}}", "query": "where is the needle",
                      "candidates": cands, "gold": ["c18", "c19"]}})
    sets.append({{"seed": s, "pools": pools}})
doc = {{"schema": "dittobench-rerank-pools-v1",
        "bench_version": int(arg("-bench-version")), "run_size": arg("-run-size"),
        "pool_size": 20, "sets": sets}}
body = b"not json" if mode == "garbage" else json.dumps(doc).encode()
pathlib.Path(arg("-out")).write_bytes(body)
"""


def artifact_dir(root: Path, *, needle_weight: float, base_model: str = "none") -> Path:
    root.mkdir(parents=True)
    (root / "model.onnx").write_bytes(build_model(needle_weight=needle_weight))
    (root / "vocab.txt").write_text(vocab_text())
    (root / "manifest.json").write_text(json.dumps(manifest(base_model=base_model)))
    return root


def make_config(tmp_path: Path, mode: str = "ok") -> ScorerConfig:
    root = tmp_path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    work = root / "work"
    work.mkdir(mode=0o700)
    binary = root / "rerank-pools"
    binary.write_text(poolgen_script(root / "poolgen.log", mode))
    binary.chmod(0o755)
    # A model that ignores every token: CE ties keep the BM25 base order, so
    # the incumbent scores exactly the base and a needle model beats it.
    incumbent = artifact_dir(root / "incumbent", needle_weight=0.0)
    return ScorerConfig(
        poolgen_executable=binary,
        poolgen_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        surface_salt=SALT,
        work_root=work,
        incumbent_path=incumbent,
        pool_seed_count=2,
        incumbent_model_sha256=hashlib.sha256(
            (incumbent / "model.onnx").read_bytes()
        ).hexdigest(),
        incumbent_vocab_sha256=hashlib.sha256(
            (incumbent / "vocab.txt").read_bytes()
        ).hexdigest(),
    )


async def submit(
    sessions: async_sessionmaker[AsyncSession],
    store: FakeStore,
    tmp_path: Path,
    *,
    needle_weight: float = 1.0,
    base_model: str = "none",
    hotkey: str = "5Challenger",
) -> UUID:
    body = tarball(
        model=build_model(needle_weight=needle_weight),
        manifest_doc=manifest(base_model=base_model),
    )
    scratch = tmp_path / f"upload-{uuid4()}.tar.gz"
    scratch.write_bytes(body)
    artifact = load_artifact(scratch)
    submission_id = uuid4()
    store.objects[artifact_key(submission_id)] = body
    async with sessions() as session, session.begin():
        await insert_reranker_submission(
            session,
            submission_id=submission_id,
            miner_hotkey=hotkey,
            miner_coldkey=None,
            upload_sha256=hashlib.sha256(body).hexdigest(),
            artifact_sha256=artifact.sha256,
            contract_sha256="c" * 64,
            size_bytes=len(body),
            base_model=base_model,
            base_model_license="mit",
        )
    return submission_id


async def row(
    sessions: async_sessionmaker[AsyncSession], submission_id: UUID
) -> RerankerSubmission:
    async with sessions() as session:
        found = await session.get(RerankerSubmission, submission_id)
        assert found is not None
        return found


async def champions(
    sessions: async_sessionmaker[AsyncSession],
) -> list[RerankerChampion]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(RerankerChampion).order_by(RerankerChampion.id)
            )
        )


def poolgen_calls(tmp_path: Path) -> list[dict]:
    log = tmp_path.resolve() / "poolgen.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_reranker_seed_is_reproducible_and_domain_separated() -> None:
    submission_id = UUID("12345678-1234-5678-1234-567812345678")
    seed = derive_reranker_seed(BLOCK.hash, submission_id, contract_id=CONTRACT_ID)
    digest = hashlib.sha256(
        f"{CONTRACT_ID}:{'ab' * 32}:{submission_id}".encode()
    ).digest()
    assert seed == int.from_bytes(digest[:8], "big") & ((1 << 62) - 1)
    # The 0x prefix and case do not matter; the domain and the block do.
    assert seed == derive_reranker_seed(
        "AB" * 32, submission_id, contract_id=CONTRACT_ID
    )
    assert seed != derive_reranker_seed(
        BLOCK.hash, submission_id, contract_id="another-season"
    )
    assert seed != derive_seed(BLOCK.hash, submission_id)
    # Headroom so seed + count - 1 always fits the generator's int64.
    assert 0 <= seed < 1 << 62


def test_crown_requires_every_gate() -> None:
    assert crown_decision(
        dethrones=True, latency_passed=True, transfer_gate="passed", require=True
    ) == (True, [])
    assert crown_decision(
        dethrones=True,
        latency_passed=True,
        transfer_gate="not_evaluated",
        require=True,
    ) == (False, ["transfer_gate_not_evaluated"])
    assert crown_decision(
        dethrones=True,
        latency_passed=True,
        transfer_gate="not_evaluated",
        require=False,
    ) == (True, [])
    assert crown_decision(
        dethrones=False, latency_passed=False, transfer_gate="passed", require=False
    ) == (False, ["no_dethrone", "latency_gate"])


def test_disabled_by_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DITTO_RERANKER_SCORING_ENABLED", raising=False)
    assert main([]) == 0
    assert capsys.readouterr().out.strip() == "disabled"


def test_salt_is_not_in_repr(tmp_path) -> None:
    assert str(SALT) not in repr(make_config(tmp_path))


async def test_scores_challenger_and_incumbent_on_the_same_pools(
    tmp_path, session_maker, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    config = make_config(tmp_path)
    store, chain = FakeStore(), FakeChain()
    submission_id = await submit(session_maker, store, tmp_path)

    assert await run_once(config, session_maker, chain, store) == "scored"
    assert await run_once(config, session_maker, chain, store) == "idle"

    scored = await row(session_maker, submission_id)
    expected_seed = derive_reranker_seed(
        BLOCK.hash, submission_id, contract_id=CONTRACT_ID
    )
    assert scored.status == "scored"
    assert (scored.seed, scored.seed_block, scored.seed_block_hash) == (
        expected_seed,
        BLOCK.number,
        "ab" * 32,
    )
    assert scored.scoring_token is None and scored.scoring_attempts == 1
    score = scored.score
    assert score is not None
    assert score["seed"] == str(expected_seed)
    assert score["pools"] == 2 * POOLS_PER_SEED
    # Challenger and champion were scored on one bundle.
    assert (
        score["challenger"]["pools_sha256"]
        == score["champion"]["pools_sha256"]
        == score["pools_sha256"]
    )
    assert score["champion"]["kind"] == "incumbent"
    assert score["champion"]["submission_id"] is None
    assert "per_pool" not in score["challenger"]
    assert score["challenger"]["score"] > score["champion"]["score"]
    assert score["comparison"]["dethrones"] is True
    assert score["latency"]["incumbent_p50_ms"] is not None
    assert score["transfer_gate"] == {"status": "not_evaluated", "required": True}
    assert score["dethrones_shadow"] is True
    # Nobody is crowned until the transfer gate (SN-226) exists.
    assert score["crowned"] is False
    assert score["crown_blocked_by"] == ["transfer_gate_not_evaluated"]
    assert await champions(session_maker) == []

    (call,) = poolgen_calls(tmp_path)
    argv = call["argv"]
    assert argv[argv.index("-seed") + 1] == str(expected_seed)
    assert argv[argv.index("-count") + 1] == "2"
    assert "-surface-salt" not in argv
    assert call["salt"] == str(SALT)
    assert set(call["env"]) <= {"LANG", "TMPDIR", "LC_CTYPE", "PWD"}

    # The held salt never leaves the owner-only file.
    leaked = [
        " ".join(argv),
        json.dumps(score),
        caplog.text,
        str(scored.invalid_reason),
        str(scored.failure_reason),
    ]
    assert not any(str(SALT) in text for text in leaked)
    assert str(tmp_path.resolve()) not in json.dumps(score)
    # The private work directory is removed after scoring.
    assert list(config.work_root.iterdir()) == []


async def test_crowns_in_shadow_when_the_transfer_gate_is_waived(
    tmp_path, session_maker
) -> None:
    config = replace(make_config(tmp_path), require_transfer_gate=False)
    store, chain = FakeStore(), FakeChain()
    first = await submit(session_maker, store, tmp_path, hotkey="5First")
    assert await run_once(config, session_maker, chain, store) == "scored"
    first_row = await row(session_maker, first)
    assert first_row.score is not None and first_row.score["crowned"] is True
    (crown,) = await champions(session_maker)
    assert crown.submission_id == first
    assert crown.previous_submission_id is None and crown.superseded_at is None
    assert crown.pools_sha256 == first_row.score["pools_sha256"]

    # The same model under another manifest ties the champion: no dethrone,
    # and the challenger is compared against the crowned submission now.
    second = await submit(
        session_maker, store, tmp_path, base_model="copy", hotkey="5Second"
    )
    assert await run_once(config, session_maker, chain, store) == "scored"
    score = (await row(session_maker, second)).score
    assert score is not None
    assert score["champion"]["kind"] == "submission"
    assert score["champion"]["submission_id"] == str(first)
    assert score["champion"]["miner_hotkey"] == "5First"
    assert score["comparison"]["dethrones"] is False
    assert score["crowned"] is False
    assert score["crown_blocked_by"] == ["no_dethrone"]
    assert [c.submission_id for c in await champions(session_maker)] == [first]


async def test_latency_gate_marks_invalid_and_never_crowns(
    tmp_path, session_maker, monkeypatch
) -> None:
    import dittobench_reranker.scorer as scorer

    real = scorer.measure_latency

    def slow(*args, **kwargs):
        report = real(*args, **kwargs)
        return replace(report, ratio_to_incumbent=9.0, passed=False)

    monkeypatch.setattr(scorer, "measure_latency", slow)
    config = replace(make_config(tmp_path), require_transfer_gate=False)
    store = FakeStore()
    submission_id = await submit(session_maker, store, tmp_path)
    assert await run_once(config, session_maker, FakeChain(), store) == "invalid"
    invalid = await row(session_maker, submission_id)
    assert invalid.status == "invalid"
    assert invalid.invalid_reason is not None
    assert invalid.invalid_reason.startswith("latency gate: p50 is 9.00x")
    assert invalid.score is not None and invalid.score["crowned"] is False
    assert invalid.score["crown_blocked_by"] == ["latency_gate"]
    assert await champions(session_maker) == []


async def test_non_finite_model_output_is_invalid(tmp_path, session_maker) -> None:
    config = make_config(tmp_path)
    store = FakeStore()
    submission_id = await submit(
        session_maker, store, tmp_path, needle_weight=float("inf")
    )
    assert await run_once(config, session_maker, FakeChain(), store) == "invalid"
    invalid = await row(session_maker, submission_id)
    assert invalid.invalid_reason is not None
    assert invalid.invalid_reason.startswith("model output rejected")
    assert invalid.score is None


async def test_digest_mismatch_refuses_before_claiming(tmp_path, session_maker) -> None:
    config = replace(make_config(tmp_path), poolgen_sha256="0" * 64)
    store = FakeStore()
    submission_id = await submit(session_maker, store, tmp_path)
    with pytest.raises(ScorerConfigError, match="digest mismatch"):
        await run_once(config, session_maker, FakeChain(), store)
    untouched = await row(session_maker, submission_id)
    assert (untouched.status, untouched.scoring_attempts) == ("accepted", 0)
    assert poolgen_calls(tmp_path) == []


async def test_incumbent_mismatch_refuses_before_claiming(
    tmp_path, session_maker
) -> None:
    config = replace(make_config(tmp_path), incumbent_model_sha256="1" * 64)
    store = FakeStore()
    submission_id = await submit(session_maker, store, tmp_path)
    with pytest.raises(ScorerConfigError, match="incumbent"):
        await run_once(config, session_maker, FakeChain(), store)
    assert (await row(session_maker, submission_id)).status == "accepted"


async def test_world_readable_work_root_refuses(tmp_path, session_maker) -> None:
    config = make_config(tmp_path)
    config.work_root.chmod(0o755)
    with pytest.raises(ScorerConfigError, match="owner-only"):
        await run_once(config, session_maker, FakeChain(), FakeStore())


async def test_chain_outage_requeues_then_fails_without_a_seed(
    tmp_path, session_maker, caplog
) -> None:
    config = replace(make_config(tmp_path), max_attempts=2)
    store, chain = FakeStore(), FakeChain(block=None)
    submission_id = await submit(session_maker, store, tmp_path)
    assert await run_once(config, session_maker, chain, store) == "requeued"
    queued = await row(session_maker, submission_id)
    assert (queued.status, queued.scoring_attempts, queued.seed) == (
        "accepted",
        1,
        None,
    )
    assert await run_once(config, session_maker, chain, store) == "failed"
    failed = await row(session_maker, submission_id)
    assert (failed.status, failed.failure_reason) == ("failed", "chain_unavailable")
    assert "10.9.8.7" not in caplog.text


async def test_pinned_seed_survives_a_retry(tmp_path, session_maker) -> None:
    store = FakeStore()
    submission_id = await submit(session_maker, store, tmp_path)
    failing = make_config(tmp_path / "a", mode="fail")
    assert await run_once(failing, session_maker, FakeChain(), store) == "requeued"
    pinned = await row(session_maker, submission_id)
    assert pinned.seed is not None and pinned.status == "accepted"

    # A later block must not re-roll the seed.
    later = FakeChain(BlockInfo(number=BLOCK.number + 100, hash="cd" * 32))
    working = make_config(tmp_path / "b")
    assert await run_once(working, session_maker, later, store) == "scored"
    scored = await row(session_maker, submission_id)
    assert later.reads == 0
    assert (scored.seed, scored.seed_block_hash) == (pinned.seed, "ab" * 32)
    assert scored.scoring_attempts == 2


@pytest.mark.parametrize(
    ("mode", "outcome", "status", "code"),
    [
        ("garbage", "failed", "failed", "pools_invalid"),
        ("wrong-seed", "failed", "failed", "pools_invalid"),
        ("fail", "requeued", "accepted", None),
    ],
)
async def test_pool_generator_failures(
    tmp_path, session_maker, mode, outcome, status, code
) -> None:
    config = make_config(tmp_path, mode=mode)
    store = FakeStore()
    submission_id = await submit(session_maker, store, tmp_path)
    assert await run_once(config, session_maker, FakeChain(), store) == outcome
    after = await row(session_maker, submission_id)
    assert (after.status, after.failure_reason) == (status, code)
    assert list(config.work_root.iterdir()) == []


async def test_missing_and_swapped_artifacts(tmp_path, session_maker) -> None:
    config = replace(make_config(tmp_path), max_attempts=1)
    store = FakeStore()
    missing = await submit(session_maker, store, tmp_path)
    del store.objects[artifact_key(missing)]
    assert await run_once(config, session_maker, FakeChain(), store) == "failed"
    assert (await row(session_maker, missing)).failure_reason == "artifact_unavailable"

    swapped = await submit(session_maker, store, tmp_path, base_model="b")
    store.objects[artifact_key(swapped)] = tarball(model=build_model(needle_weight=2.0))
    assert await run_once(config, session_maker, FakeChain(), store) == "failed"
    assert (await row(session_maker, swapped)).failure_reason == "artifact_mismatch"


async def test_expired_lease_is_reclaimed_and_the_old_token_is_fenced(
    tmp_path, session_maker
) -> None:
    store = FakeStore()
    submission_id = await submit(session_maker, store, tmp_path)
    t0 = datetime.now(UTC)
    lease = timedelta(minutes=10)
    async with session_maker() as session, session.begin():
        first = await claim_reranker_submission(
            session, now=t0, lease=lease, max_attempts=2
        )
    assert first is not None and first.submission_id == submission_id
    async with session_maker() as session, session.begin():
        assert (
            await claim_reranker_submission(
                session, now=t0 + timedelta(minutes=5), lease=lease, max_attempts=2
            )
            is None
        )
    async with session_maker() as session, session.begin():
        second = await claim_reranker_submission(
            session, now=t0 + timedelta(minutes=11), lease=lease, max_attempts=2
        )
    assert second is not None
    assert second.submission_id == submission_id and second.token != first.token
    assert second.attempts == 2
    with pytest.raises(StaleRerankerClaimError):
        async with session_maker() as session, session.begin():
            await finish_reranker_submission(
                session,
                claim=first,
                now=t0,
                status="failed",
                failure_reason="internal_error",
            )
    # The second lease also expires, with no attempts left: failed, visibly.
    async with session_maker() as session, session.begin():
        assert (
            await claim_reranker_submission(
                session, now=t0 + timedelta(minutes=30), lease=lease, max_attempts=2
            )
            is None
        )
    exhausted = await row(session_maker, submission_id)
    assert (exhausted.status, exhausted.failure_reason) == (
        "failed",
        "attempts_exhausted",
    )
    assert exhausted.scoring_token is None
