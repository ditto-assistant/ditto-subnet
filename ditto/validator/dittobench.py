"""Async client for the hosted dittobench-api scoring engine.

Drives the run_size pipeline over HTTP: ``POST /v1/submit`` with the platform's
presigned ``tarball_url`` (mode B), then polls
``GET /v1/runs/{id}`` until the job is ``done`` and parses the ``ScoreReport``.

The returned report is the platform :class:`ScoreReport` shape (the dittobench
wire contract is identical by design), so it round-trips straight back into
``POST /validator/agent/{id}/score``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from ditto.api_models.benchmark_progress import (
    MAX_BENCHMARK_CHECKS,
    BenchmarkProgressStage,
)
from ditto.api_models.validator import ScoreReport
from ditto.validator.errors import DittobenchError, ValidatorInfrastructureError

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

# Terminal job states reported by dittobench-api's store.
_DONE = "done"
_FAILED = "failed"

_PROGRESS_STAGE_BY_STATUS: dict[str, BenchmarkProgressStage] = {
    "queued": "preparing",
    "building": "building_harness",
    "generating": "starting_harness",
    "seeding": "running_benchmark",
    "running": "running_benchmark",
    "scoring": "finalizing",
    "done": "finalizing",
    "failed": "failed_retrying",
}
_STABLE_COUNT_STATUSES = {"running", "scoring", "done"}


def _is_embedding_infrastructure_failure(error: str) -> bool:
    """Identify scorer failures from the validator-owned Ollama route."""
    normalized = error.lower()
    return (
        "host.docker.internal:11434" in normalized
        or "ollama embed request" in normalized
        or ("ollama" in normalized and "embedding" in normalized)
    )


@dataclass(frozen=True)
class DittobenchProgressSnapshot:
    """Allowlisted progress extracted from an otherwise private scorer job."""

    stage: BenchmarkProgressStage
    completed: int | None = None
    total: int | None = None


ProgressCallback = Callable[[DittobenchProgressSnapshot], Awaitable[None]]


def safe_progress_snapshot(payload: object) -> DittobenchProgressSnapshot | None:
    """Extract only status and aggregate counts from a DittoBench poll response.

    Pre-running totals can change while the generated suite is assembled, so
    counts remain unknown until the raw scorer reaches ``running``. Malformed
    counts degrade to unknown without affecting the benchmark.
    """
    if not isinstance(payload, dict):
        return None
    raw_status = payload.get("status")
    if not isinstance(raw_status, str):
        return None
    stage = _PROGRESS_STAGE_BY_STATUS.get(raw_status)
    if stage is None or raw_status not in _STABLE_COUNT_STATUSES:
        return None if stage is None else DittobenchProgressSnapshot(stage=stage)

    raw_progress = payload.get("progress")
    if not isinstance(raw_progress, dict):
        return DittobenchProgressSnapshot(stage=stage)
    completed = raw_progress.get("done")
    total = raw_progress.get("total")
    if (
        type(completed) is not int
        or type(total) is not int
        or completed < 0
        or total < 1
        or completed > total
        or total > MAX_BENCHMARK_CHECKS
    ):
        return DittobenchProgressSnapshot(stage=stage)
    return DittobenchProgressSnapshot(stage=stage, completed=completed, total=total)


class DittobenchClient:
    """HTTP client for one dittobench-api base URL."""

    def __init__(self, config: ValidatorConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        # Raw, opaque ``details`` blob from the most recent scored run (bench
        # version, paraphrase/injection telemetry, token totals). Not part of the
        # signed/DB ScoreReport contract — captured here only so the validator can
        # surface it in aggregate W&B telemetry.
        self.last_details: dict[str, object] = {}

    async def preflight(self) -> None:
        """Verify the functional embedding route before claiming a ticket.

        The configured URL is the sandbox-docker forwarder's port 11434, the
        same listener reached as ``host.docker.internal:11434`` by inner miner
        harnesses. A real embedding request checks the forwarder, Ollama, and
        the loaded model rather than merely checking container liveness.
        """
        if self._config.dittobench_mock:
            return
        try:
            response = await self._client.post(
                self._config.embed_preflight_url,
                json={"model": "embeddinggemma", "input": "validator preflight"},
                timeout=self._config.embed_preflight_timeout_seconds,
            )
        except httpx.TimeoutException as e:
            raise ValidatorInfrastructureError(
                "embedding preflight timed out through the harness forwarder"
            ) from e
        except httpx.HTTPError as e:
            raise ValidatorInfrastructureError(
                f"embedding preflight could not reach the harness forwarder: {e}"
            ) from e
        if response.status_code != 200:
            raise ValidatorInfrastructureError(
                "embedding preflight through the harness forwarder rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            embeddings = response.json().get("embeddings")
        except (ValueError, AttributeError) as e:
            raise ValidatorInfrastructureError(
                "embedding preflight returned an invalid response"
            ) from e
        if (
            not isinstance(embeddings, list)
            or not embeddings
            or not isinstance(embeddings[0], list)
            or not embeddings[0]
        ):
            raise ValidatorInfrastructureError(
                "embedding preflight returned no embedding vector"
            )

    async def score_tarball(
        self,
        *,
        tarball_url: str,
        tarball_sha256: str | None = None,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ScoreReport:
        """Score a submission by its presigned tarball URL (mode B).

        Submits the scoring inputs, then polls until the run finishes.
        Raises :class:`DittobenchError` on a failed run or the overall timeout.

        ``tarball_sha256`` (the digest the platform registered at upload) is
        forwarded so the scorer re-verifies the fetched bytes against it and
        pins the Docker build tag to the content hash.

        ``seed`` pins the dataset seed. ``dataset_sha256`` selects the CANONICAL
        validator path: when set, this posts to dittobench-api **/v1/score** with
        the platform-pinned ``seed`` + ``dataset_sha256`` (+ ``run_size``), so the
        engine regenerates that exact dataset and FAILS the run on a hash mismatch
        (tamper-evidence — every k=3 validator provably scored the platform's
        dataset). Without ``dataset_sha256`` it uses /v1/submit (practice /
        version-bump re-score, fresh-or-CRN seed).
        """
        if self._config.dittobench_mock:
            self.last_details = {}
            return self._mock_report()
        run_id = await self._submit(
            tarball_url=tarball_url,
            tarball_sha256=tarball_sha256,
            seed=seed,
            dataset_sha256=dataset_sha256,
            run_size=run_size,
        )
        return await self._poll(run_id, progress_callback=progress_callback)

    def _mock_report(self) -> ScoreReport:
        """Canned report for ``VALIDATOR_DITTOBENCH_MOCK`` (local plumbing tests)."""
        logger.info("dittobench mock enabled: returning canned ScoreReport")
        return ScoreReport(
            run_id=f"mock-{uuid4().hex[:12]}",
            seed=0,
            composite=0.9,
            tool_mean=0.9,
            memory_mean=0.9,
            median_ms=100,
            n=10,
            generated_at=datetime.now(UTC),
            per_case=[],
            structural_fingerprint=None,
            details=None,
        )

    async def _submit(
        self,
        *,
        tarball_url: str,
        tarball_sha256: str | None = None,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
    ) -> str:
        body: dict[str, object] = {
            "tarball_url": tarball_url,
            "run_size": run_size or self._config.run_size,
        }
        if tarball_sha256:
            body["tarball_sha256"] = tarball_sha256
        if seed is not None:
            body["seed"] = seed
        # Canonical validator path: pin the dataset so the engine fails on a
        # regenerated-hash mismatch. Otherwise the practice/re-score path.
        if dataset_sha256:
            body["dataset_sha256"] = dataset_sha256
            endpoint = "/v1/score"
        else:
            endpoint = "/v1/submit"
        url = f"{self._config.dittobench_api_url}{endpoint}"
        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            raise DittobenchError(f"submit failed: {e}") from e
        if resp.status_code not in (200, 202):
            raise DittobenchError(
                f"submit rejected ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        run_id = data.get("run_id")
        if not run_id:
            raise DittobenchError("submit response missing run_id")
        logger.info("dittobench run %s started for tarball", run_id)
        return str(run_id)

    async def _poll(
        self, run_id: str, *, progress_callback: ProgressCallback | None = None
    ) -> ScoreReport:
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        deadline = self._config.dittobench_timeout_seconds
        waited = 0.0
        try:
            while waited <= deadline:
                resp = await self._client.get(url)
                if resp.status_code != 200:
                    raise DittobenchError(
                        f"poll rejected ({resp.status_code}): {resp.text[:200]}"
                    )
                data = resp.json()
                if not isinstance(data, dict):
                    raise DittobenchError("poll response was not a JSON object")
                snapshot = safe_progress_snapshot(data)
                if snapshot is not None and progress_callback is not None:
                    try:
                        await progress_callback(snapshot)
                    except Exception:  # noqa: BLE001 - telemetry never gates scoring
                        logger.warning(
                            "dittobench progress callback failed; scoring continues"
                        )
                status = data.get("status")
                if status == _DONE:
                    rep = data.get("report")
                    self.last_details = (
                        rep["details"]
                        if isinstance(rep, dict)
                        and isinstance(rep.get("details"), dict)
                        else {}
                    )
                    return self._parse_report(data)
                if status == _FAILED:
                    error = str(data.get("error", "unknown"))
                    if _is_embedding_infrastructure_failure(error):
                        raise ValidatorInfrastructureError(
                            f"run {run_id} lost validator embedding infrastructure: "
                            f"{error}"
                        )
                    raise DittobenchError(f"run {run_id} failed: {error}")
                await asyncio.sleep(self._config.dittobench_poll_seconds)
                waited += self._config.dittobench_poll_seconds
        except httpx.HTTPError as e:
            raise DittobenchError(f"poll failed: {e}") from e
        except asyncio.CancelledError:
            await self._cancel(run_id)
            raise
        await self._cancel(run_id)
        raise DittobenchError(
            f"run {run_id} did not finish within "
            f"{self._config.dittobench_timeout_seconds}s"
        )

    async def _cancel(self, run_id: str) -> None:
        """Best-effort cancellation so a timed-out run cannot keep the sandbox.

        Older scorer revisions do not expose DELETE yet; a failed cancellation
        is logged but never hides the original validator timeout.
        """
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        try:
            resp = await self._client.delete(url)
            if resp.status_code not in (200, 202, 404, 405):
                logger.warning(
                    "dittobench run %s cancellation rejected (%d): %s",
                    run_id,
                    resp.status_code,
                    resp.text[:200],
                )
        except httpx.HTTPError as e:
            logger.warning("dittobench run %s cancellation failed: %s", run_id, e)

    @staticmethod
    def _parse_report(job: dict) -> ScoreReport:
        report = job.get("report")
        if not isinstance(report, dict):
            raise DittobenchError("done run missing report object")
        # The dittobench ScoreReport omits the seed (it lives on the job); the
        # platform ScoreReport carries it, so inject it before validating.
        report.setdefault("seed", job.get("seed", 0))
        try:
            return ScoreReport.model_validate(report)
        except Exception as e:  # noqa: BLE001 - surface any shape drift as our error
            raise DittobenchError(f"could not parse ScoreReport: {e}") from e
