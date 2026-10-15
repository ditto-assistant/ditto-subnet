"""Deterministic reranker scorer (SN-205).

For each pool: encode every (query, candidate) pair with the product tokenizer,
run one batched ONNX forward pass, fuse with the base order exactly as the
product does, and compute NDCG@10 and R@10. The score is the mean NDCG@10.

Validators run this with one intra-op thread so the logits, and therefore every
rank, are identical across runs of the same build. Latency is a separate,
trusted measurement (``measure_latency``) because it depends on hardware.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass

import numpy as np

from dittobench_reranker.artifact import Artifact
from dittobench_reranker.contract import (
    CONTRACT,
    MODEL_INPUT_NAMES,
    MODEL_OUTPUT_NAME,
    SeasonContract,
    dethrone_threshold,
)
from dittobench_reranker.pools import Pool, PoolBundle
from dittobench_reranker.ranking import fuse_rrf, ndcg_at_k, recall_at_k
from dittobench_reranker.tokenizer import PAD_ID, WordPiece


class ScoringError(RuntimeError):
    """The model ran but produced output the contract does not allow."""


def make_session(model: bytes, threads: int):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.log_severity_level = 3
    return ort.InferenceSession(
        model, sess_options=opts, providers=["CPUExecutionProvider"]
    )


def encode_pool(
    tok: WordPiece, query: str, texts: tuple[str, ...], max_len: int
) -> dict[str, np.ndarray]:
    """Pad a pool into one batch, as backend crossencoder.Score does."""
    encoded = [tok.encode_pair(query, t, max_len) for t in texts]
    seq = max(len(ids) for ids, _ in encoded)
    batch = len(encoded)
    ids = np.full((batch, seq), PAD_ID, dtype=np.int64)
    mask = np.zeros((batch, seq), dtype=np.int64)
    types = np.zeros((batch, seq), dtype=np.int64)
    for i, (row_ids, row_types) in enumerate(encoded):
        n = len(row_ids)
        ids[i, :n] = row_ids
        mask[i, :n] = 1
        types[i, :n] = row_types
    return dict(zip(MODEL_INPUT_NAMES, (ids, mask, types), strict=True))


def run_pool(session, feeds: dict[str, np.ndarray]) -> list[float]:
    (logits,) = session.run([MODEL_OUTPUT_NAME], feeds)
    logits = np.asarray(logits)
    batch = feeds[MODEL_INPUT_NAMES[0]].shape[0]
    if logits.shape != (batch, 1):
        raise ScoringError(f"logits shape {logits.shape}, want ({batch}, 1)")
    out = logits.astype(np.float32).reshape(-1)
    if not np.all(np.isfinite(out)):
        raise ScoringError("model produced a non-finite logit")
    return [float(x) for x in out]


@dataclass(frozen=True)
class PoolResult:
    pool_id: str
    ndcg: float
    recall: float
    base_ndcg: float


@dataclass(frozen=True)
class ScoreReport:
    contract_id: str
    contract_sha256: str
    artifact_sha256: str
    pools_sha256: str
    pools: int
    score: float
    recall_at_10: float
    base_ndcg_at_10: float
    lift: float
    per_pool: tuple[PoolResult, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["per_pool"] = [asdict(p) for p in self.per_pool]
        return data


def score(
    artifact: Artifact, bundle: PoolBundle, contract: SeasonContract = CONTRACT
) -> ScoreReport:
    params = contract.scoring
    tok = WordPiece.from_text(artifact.vocab.decode("utf-8"))
    session = make_session(artifact.model, params.scoring_threads)
    results: list[PoolResult] = []
    for pool in bundle.pools:
        results.append(_score_pool(session, tok, pool, contract))
    n = len(results)
    mean = math.fsum(r.ndcg for r in results) / n
    base = math.fsum(r.base_ndcg for r in results) / n
    return ScoreReport(
        contract_id=contract.contract_id,
        contract_sha256=contract.digest(),
        artifact_sha256=artifact.sha256,
        pools_sha256=bundle.sha256,
        pools=n,
        score=mean,
        recall_at_10=math.fsum(r.recall for r in results) / n,
        base_ndcg_at_10=base,
        lift=mean - base,
        per_pool=tuple(results),
    )


def _score_pool(
    session, tok: WordPiece, pool: Pool, contract: SeasonContract
) -> PoolResult:
    p = contract.scoring
    feeds = encode_pool(tok, pool.query, pool.candidate_texts, p.max_len)
    ce = run_pool(session, feeds)
    order = [pool.candidate_ids[i] for i in fuse_rrf(ce, p.rrf_k, p.ce_weight)]
    gold = set(pool.gold)
    return PoolResult(
        pool_id=pool.pool_id,
        ndcg=ndcg_at_k(order, gold, p.ndcg_k),
        recall=recall_at_k(order, gold, p.recall_k),
        base_ndcg=ndcg_at_k(pool.candidate_ids, gold, p.ndcg_k),
    )


@dataclass(frozen=True)
class LatencyReport:
    threads: int
    measured_pools: int
    p50_ms: float
    p90_ms: float
    # Set when the incumbent was timed alongside; the gate is then the ratio.
    incumbent_p50_ms: float | None
    ratio_to_incumbent: float | None
    max_ratio: float
    budget_ms: float
    passed: bool


def _p90(samples: list[float]) -> float:
    if len(samples) >= 10:
        return statistics.quantiles(samples, n=10)[-1]
    return max(samples)


def measure_latency(
    artifact: Artifact,
    bundle: PoolBundle,
    contract: SeasonContract = CONTRACT,
    incumbent: Artifact | None = None,
) -> LatencyReport:
    """p50 wall time of one tokenized, batched forward pass per pool.

    Tokenization is included because the product pays for it on the read
    path. With ``incumbent`` the two models are timed on the same pools,
    interleaved pool by pool, and the gate is the p50 ratio, which does not
    depend on the CPU. Without it the gate falls back to the absolute budget,
    which is only meaningful on reference hardware.
    """
    gate = contract.latency
    params = contract.scoring
    contenders = [artifact] + ([incumbent] if incumbent is not None else [])
    runners = [
        (
            WordPiece.from_text(a.vocab.decode("utf-8")),
            make_session(a.model, gate.threads),
        )
        for a in contenders
    ]
    pools = list(bundle.pools)
    needed = gate.warmup_pools + gate.measured_pools
    if len(pools) < needed:
        pools = (pools * (needed // len(pools) + 1))[:needed]
    samples: list[list[float]] = [[] for _ in runners]
    for i, pool in enumerate(pools[:needed]):
        for j, (tok, session) in enumerate(runners):
            start = time.perf_counter()
            feeds = encode_pool(tok, pool.query, pool.candidate_texts, params.max_len)
            run_pool(session, feeds)
            elapsed = (time.perf_counter() - start) * 1000.0
            if i >= gate.warmup_pools:
                samples[j].append(elapsed)
    p50 = statistics.median(samples[0])
    incumbent_p50 = statistics.median(samples[1]) if incumbent is not None else None
    ratio = p50 / incumbent_p50 if incumbent_p50 else None
    passed = (
        ratio <= gate.max_ratio_to_incumbent
        if ratio is not None
        else p50 <= gate.budget_ms_p50
    )
    return LatencyReport(
        threads=gate.threads,
        measured_pools=len(samples[0]),
        p50_ms=p50,
        p90_ms=_p90(samples[0]),
        incumbent_p50_ms=incumbent_p50,
        ratio_to_incumbent=ratio,
        max_ratio=gate.max_ratio_to_incumbent,
        budget_ms=gate.budget_ms_p50,
        passed=passed,
    )


@dataclass(frozen=True)
class Comparison:
    pools: int
    champion: float
    challenger: float
    mean_diff: float
    stderr: float
    threshold: float
    dethrones: bool


def compare(champion: ScoreReport, challenger: ScoreReport) -> Comparison:
    """Paired dethrone test on the pools both reports share."""
    if champion.pools_sha256 != challenger.pools_sha256:
        raise ValueError("reports were scored on different pools")
    if champion.contract_sha256 != challenger.contract_sha256:
        raise ValueError("reports were scored under different contracts")
    base = {p.pool_id: p.ndcg for p in champion.per_pool}
    diffs = [p.ndcg - base[p.pool_id] for p in challenger.per_pool if p.pool_id in base]
    if len(diffs) < 2:
        raise ValueError("need at least two shared pools")
    mean_diff = math.fsum(diffs) / len(diffs)
    stderr = statistics.stdev(diffs) / math.sqrt(len(diffs))
    threshold = dethrone_threshold(stderr)
    return Comparison(
        pools=len(diffs),
        champion=champion.score,
        challenger=challenger.score,
        mean_diff=mean_diff,
        stderr=stderr,
        threshold=threshold,
        dethrones=mean_diff >= threshold,
    )
