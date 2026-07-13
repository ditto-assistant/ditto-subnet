"""Async client for the hosted dittobench-api scoring engine.

Drives the run_size pipeline over HTTP: ``POST /v1/submit`` with the platform's
presigned ``tarball_url`` (mode B) + the BYOK OpenRouter key, then polls
``GET /v1/runs/{id}`` until the job is ``done`` and parses the ``ScoreReport``.

The returned report is the platform :class:`ScoreReport` shape (the dittobench
wire contract is identical by design), so it round-trips straight back into
``POST /validator/agent/{id}/score``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from ditto.api_models.validator import ScoreReport
from ditto.validator.errors import DittobenchError

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

# Terminal job states reported by dittobench-api's store.
_DONE = "done"
_FAILED = "failed"


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

    async def score_tarball(
        self,
        *,
        tarball_url: str,
        tarball_sha256: str | None = None,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
    ) -> ScoreReport:
        """Score a submission by its presigned tarball URL (mode B).

        Submits with the BYOK OpenRouter key, then polls until the run finishes.
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
        return await self._poll(run_id)

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
            "openrouter_key": self._config.openrouter_key,
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

    async def _poll(self, run_id: str) -> ScoreReport:
        url = f"{self._config.dittobench_api_url}/v1/runs/{run_id}"
        deadline = self._config.dittobench_timeout_seconds
        waited = 0.0
        while waited <= deadline:
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError as e:
                raise DittobenchError(f"poll failed: {e}") from e
            if resp.status_code != 200:
                raise DittobenchError(
                    f"poll rejected ({resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            status = data.get("status")
            if status == _DONE:
                rep = data.get("report")
                self.last_details = (
                    rep["details"]
                    if isinstance(rep, dict) and isinstance(rep.get("details"), dict)
                    else {}
                )
                return self._parse_report(data)
            if status == _FAILED:
                raise DittobenchError(
                    f"run {run_id} failed: {data.get('error', 'unknown')}"
                )
            await asyncio.sleep(self._config.dittobench_poll_seconds)
            waited += self._config.dittobench_poll_seconds
        raise DittobenchError(
            f"run {run_id} did not finish within "
            f"{self._config.dittobench_timeout_seconds}s"
        )

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
