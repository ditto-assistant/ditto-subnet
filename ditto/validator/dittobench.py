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

    async def score_tarball(self, *, tarball_url: str) -> ScoreReport:
        """Score a submission by its presigned tarball URL (mode B).

        Submits at ``run_size`` (default ``full``) with the BYOK OpenRouter key,
        then polls until the run finishes. Raises :class:`DittobenchError` on a
        failed run or when the overall timeout elapses.
        """
        if self._config.dittobench_mock:
            return self._mock_report()
        run_id = await self._submit(tarball_url=tarball_url)
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
        )

    async def _submit(self, *, tarball_url: str) -> str:
        body = {
            "tarball_url": tarball_url,
            "run_size": self._config.run_size,
            "openrouter_key": self._config.openrouter_key,
        }
        url = f"{self._config.dittobench_api_url}/v1/submit"
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
