"""The screener sweep loop.

One sweep: lease one eligible agent from the platform, screen it through the
build gate, and post a lease-bound signed verdict. Agents are processed one at
a time because builds are heavy and serial execution keeps host load predictable.

A single bad submission or a transient platform error must never stall the loop:
each agent is guarded, and a failed platform call is logged and retried next
sweep. The loop drains promptly when the queue is non-empty and sleeps
``poll_seconds`` when it is idle, exiting cleanly when ``stop`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from ditto_screener import __version__
from ditto_screener.errors import PlatformError
from ditto_screener.gate import LeaseDeadline
from ditto_screener.heartbeat import (
    ReviewSettingsStatus,
    ScreenerHeartbeatRequest,
    ScreenerProgress,
    ScreenerProgressStage,
    ScreenerRuntimeState,
)
from ditto_screener.policy import (
    ScreeningOutcome,
    SourceReviewObservation,
    builtin_policy_manifest,
    core_decision,
)
from ditto_screener.review_settings import (
    ShadowReviewObservationRequest,
    ShadowReviewUsage,
    bootstrap_review_settings,
)
from ditto_screener.signing import sign_heartbeat, sign_verdict
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    ScreenerQueueItem,
    ScreenEvidenceItem,
    ScreenResultOutcome,
    ScreenReviewAudit,
    SourceReviewFinding,
)

if TYPE_CHECKING:
    from uuid import UUID

    from ditto_screener.config import ScreenerConfig
    from ditto_screener.gate import BuildGate, BuiltImageArtifact
    from ditto_screener.heartbeat import SystemMetricsCollector
    from ditto_screener.l2_review import L2RunResult
    from ditto_screener.platform import PlatformClient

logger = logging.getLogger(__name__)

EXACT_CROSS_MINER_DUPLICATE = "exact-cross-miner-duplicate"

_HEARTBEAT_PROTOCOL_VERSION = 5


def _resolve_instance_id() -> str:
    """Stable per-worker id for the heartbeat (the fleet shares one hotkey).

    On GCE ``gethostname()`` is the instance name (``ditto-screener-prod``,
    ``ditto-screener-fleet-xxxx``). Sanitized to the signed instance_id charset
    (no ':', <=63 chars) so an odd hostname can never break the signing message.
    """
    label = (socket.gethostname() or "screener").split(".", 1)[0]
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "-", label)[:63].strip("-")
    return cleaned or "screener"


_HEARTBEAT_MIN_INTERVAL_SECONDS = 120.0
_ACTIVE_HEARTBEAT_SECONDS = 120.0
# Slice of the lease reserved only for signing and POSTing the verdict. Export,
# multipart upload, and full-byte platform verification are part of the gate and
# must finish before its deadline; they do not consume this final response tail.
_LEASE_SUBMIT_MARGIN_SECONDS = 30.0


class ScreenerWorker:
    """Drains the screener queue, gating each agent and posting a verdict."""

    def __init__(
        self,
        *,
        config: ScreenerConfig,
        platform: PlatformClient,
        gate: BuildGate,
        keypair: Any,
        system_metrics: SystemMetricsCollector | None = None,
    ) -> None:
        self._config = config
        self._platform = platform
        self._gate = gate
        self._keypair = keypair
        self._system_metrics = system_metrics
        self._instance_id = config.node_id or _resolve_instance_id()
        self._active_agent_id: UUID | None = None
        self._active_progress_stage: ScreenerProgressStage | None = None
        self._active_lease_deadline: LeaseDeadline | None = None
        self._job_started_at: int | None = None
        self._last_heartbeat_timestamp = 0
        self._last_heartbeat_monotonic = float("-inf")
        self._last_heartbeat_state: ScreenerRuntimeState | None = None
        self._progress_heartbeat_tasks: set[asyncio.Task[None]] = set()
        bootstrap = bootstrap_review_settings(config)
        bootstrap_manifest = builtin_policy_manifest(
            bootstrap.settings.policy_manifest_profile,
            bootstrap.settings.policy_manifest_rotation_id,
        )
        self._review_settings_status = ReviewSettingsStatus(
            revision=bootstrap.revision,
            scope=bootstrap.scope,
            mode=bootstrap.settings.mode,
            checksum=bootstrap.checksum,
            source="bootstrap",
            policy_manifest_profile=bootstrap.settings.policy_manifest_profile,
            policy_manifest_rotation_id=bootstrap.settings.policy_manifest_rotation_id,
            policy_manifest_digest=bootstrap_manifest.digest,
        )

    def _set_progress(self, stage: ScreenerProgressStage) -> None:
        """Advance public-safe progress without waiting on telemetry I/O."""
        if self._active_agent_id is None or self._job_started_at is None:
            return
        self._active_progress_stage = stage
        progress = ScreenerProgress(stage=stage, started_at=self._job_started_at)
        task = asyncio.create_task(
            self._report_heartbeat("screening", force=True, progress_override=progress)
        )
        self._progress_heartbeat_tasks.add(task)
        task.add_done_callback(self._progress_heartbeat_tasks.discard)

    def _screen_deadline(self, lease_deadline: datetime | None) -> LeaseDeadline | None:
        """Monotonic budget for one screen, or ``None`` when the lease is open.

        Converts the platform's wall-clock ``lease_deadline`` into a
        ``loop.time()`` bound and reserves ``_LEASE_SUBMIT_MARGIN_SECONDS`` for
        signing and posting the verdict. A past/near deadline yields a bound in
        the past so the caller skips the build and reports retryable at once.
        """
        if lease_deadline is None:
            return None
        remaining = (
            lease_deadline - datetime.now(UTC)
        ).total_seconds() - _LEASE_SUBMIT_MARGIN_SECONDS
        return LeaseDeadline(asyncio.get_running_loop().time() + remaining)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sweep until ``stop`` is set, sleeping when the queue is empty."""
        logger.info(
            "screener worker started hotkey=%s netuid=%d platform=%s",
            self._config.screener_hotkey,
            self._config.netuid,
            self._config.platform_api_url,
        )
        while not stop.is_set():
            await self._report_heartbeat("polling")
            try:
                processed = await self._sweep(stop)
            except PlatformError as e:
                logger.warning("sweep failed (retrying next cycle): %s", e)
                processed = 0
            if processed == 0 and not stop.is_set():
                await self._sleep_or_stop(stop, self._config.poll_seconds)
        logger.info("screener worker stopped")

    async def _report_heartbeat(
        self,
        state: ScreenerRuntimeState,
        *,
        force: bool = False,
        progress_override: ScreenerProgress | None = None,
    ) -> None:
        """Publish privacy-bounded fleet health without gating screening."""
        now_monotonic = time.monotonic()
        if (
            not force
            and state == self._last_heartbeat_state
            and now_monotonic - self._last_heartbeat_monotonic
            < _HEARTBEAT_MIN_INTERVAL_SECONDS
        ):
            return
        try:
            timestamp = max(int(time.time()), self._last_heartbeat_timestamp + 1)
            # Allocate before network I/O so concurrent best-effort stage reports
            # remain strictly ordered even if they arrive out of order.
            self._last_heartbeat_timestamp = timestamp
            metrics = (
                self._system_metrics.collect()
                if self._system_metrics is not None
                else None
            )
            progress = progress_override or (
                ScreenerProgress(
                    stage=self._active_progress_stage,
                    started_at=self._job_started_at,
                )
                if state == "screening"
                and self._active_progress_stage is not None
                and self._job_started_at is not None
                else None
            )
            signature = sign_heartbeat(
                self._keypair,
                screener_hotkey=self._config.screener_hotkey,
                software_version=__version__,
                protocol_version=_HEARTBEAT_PROTOCOL_VERSION,
                policy_version=SCREENING_POLICY_VERSION,
                state=state,
                active_agent_id=self._active_agent_id,
                instance_id=self._instance_id,
                progress=progress,
                system_metrics=metrics,
                review_settings=self._review_settings_status,
                timestamp=timestamp,
            )
            request = ScreenerHeartbeatRequest(
                screener_hotkey=self._config.screener_hotkey,
                software_version=__version__,
                protocol_version=_HEARTBEAT_PROTOCOL_VERSION,
                policy_version=SCREENING_POLICY_VERSION,
                state=state,
                active_agent_id=self._active_agent_id,
                instance_id=self._instance_id,
                progress=progress,
                system_metrics=metrics,
                review_settings=self._review_settings_status,
                timestamp=timestamp,
                signature=signature,
            )
            response = await self._platform.submit_heartbeat(request)
            if (
                response.accepted
                and response.lease_deadline is not None
                and self._active_lease_deadline is not None
            ):
                renewed = self._screen_deadline(response.lease_deadline)
                if renewed is not None:
                    self._active_lease_deadline.renew(renewed.expires_at)
        except Exception as error:  # noqa: BLE001 - observability is best effort
            logger.warning("screener heartbeat failed (screening continues): %s", error)
        finally:
            # Throttle an older platform that has not deployed the optional
            # heartbeat endpoint yet; mixed deployment states remain safe.
            self._last_heartbeat_monotonic = now_monotonic
            self._last_heartbeat_state = state

    async def _heartbeat_while_active(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_ACTIVE_HEARTBEAT_SECONDS)
            except TimeoutError:
                await self._report_heartbeat("screening", force=True)

    async def _sweep(self, stop: asyncio.Event) -> int:
        """Lease and screen the next eligible agent; return how many were done."""
        review_settings = await self._platform.get_review_settings(self._instance_id)
        self._gate.apply_review_settings(review_settings)
        manifest = builtin_policy_manifest(
            review_settings.settings.policy_manifest_profile,
            review_settings.settings.policy_manifest_rotation_id,
        )
        self._review_settings_status = ReviewSettingsStatus(
            revision=review_settings.revision,
            scope=review_settings.scope,
            mode=review_settings.settings.mode,
            checksum=review_settings.checksum,
            source=self._platform.review_settings_source,
            policy_manifest_profile=review_settings.settings.policy_manifest_profile,
            policy_manifest_rotation_id=review_settings.settings.policy_manifest_rotation_id,
            policy_manifest_digest=manifest.digest,
        )
        required_policy = await self._platform.get_required_policy_version()
        if required_policy > SCREENING_POLICY_VERSION:
            raise PlatformError(
                "screening policy newer than this build before claim: platform "
                f"requires {required_policy}, worker supports "
                f"{SCREENING_POLICY_VERSION}. The platform activates each policy "
                "version on a schedule; deploy a build implementing the target "
                "version before its activation time."
            )
        if required_policy < SCREENING_FLOOR_POLICY_VERSION:
            raise PlatformError(
                "screening policy older than this build supports before claim: "
                f"platform requires {required_policy}, worker supports "
                f"{SCREENING_FLOOR_POLICY_VERSION}-{SCREENING_POLICY_VERSION}"
            )
        # A mixed-fleet platform may require the older policy during a
        # scheduled activation window; screen under exactly what it requires.
        screen_version = min(required_policy, SCREENING_POLICY_VERSION)
        queue = await self._platform.claim_next(policy_version=screen_version)
        if queue.required_policy_version != required_policy:
            raise PlatformError(
                "platform changed screening policy during claim: expected "
                f"{required_policy}, received {queue.required_policy_version}"
            )
        if not queue.items:
            return 0
        logger.info("screener sweep: %d agent(s) to screen", len(queue.items))
        done = 0
        for item in queue.items:
            if stop.is_set():
                break
            await self._screen_one(item, policy_version=screen_version)
            done += 1
        return done

    async def _screen_one(
        self, item: ScreenerQueueItem, *, policy_version: int
    ) -> None:
        """Gate one agent and post its signed verdict. Never raises."""
        agent_id = item.agent_id
        if item.attempt_id is None:
            logger.error("claimed agent_id=%s without a screening attempt id", agent_id)
            return
        attempt_id = item.attempt_id
        self._active_agent_id = agent_id
        self._active_lease_deadline = self._screen_deadline(item.lease_deadline)
        self._job_started_at = int(time.time())
        self._set_progress("preparing")
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_while_active(heartbeat_stop)
        )
        try:
            screened_image: BuiltImageArtifact | None = None
            screened_image_upload_id: UUID | None = None

            async def publish_image(image: BuiltImageArtifact) -> None:
                nonlocal screened_image, screened_image_upload_id
                screened_image_upload_id = await self._platform.upload_screened_image(
                    agent_id,
                    attempt_id=attempt_id,
                    path=image.path,
                    sha256=image.sha256,
                    size_bytes=image.size_bytes,
                    image_id=image.image_id,
                    image_ref=image.image_ref,
                )
                screened_image = image

            if item.precheck_reason_code is not None:
                if item.precheck_reason_code != EXACT_CROSS_MINER_DUPLICATE:
                    raise PlatformError(
                        "unsupported platform precheck disposition: "
                        f"{item.precheck_reason_code}"
                    )
                result = core_decision(
                    ScreeningOutcome.DETERMINISTIC_REJECT,
                    code=EXACT_CROSS_MINER_DUPLICATE,
                    summary="artifact is an exact cross-miner duplicate",
                    detail="exact cross-miner duplicate",
                )
            else:
                screen_deadline = self._active_lease_deadline
                if (
                    screen_deadline is not None
                    and screen_deadline.expires_at <= asyncio.get_running_loop().time()
                ):
                    logger.warning(
                        "agent_id=%s claimed with insufficient lease budget; "
                        "reporting infrastructure failure for manual retry",
                        agent_id,
                    )
                    result = core_decision(
                        ScreeningOutcome.RETRYABLE_INFRA,
                        code="lease-budget-exhausted",
                        summary="insufficient screening lease budget at claim",
                        detail="screener error: insufficient lease budget at claim",
                    )
                else:
                    artifact = await self._platform.get_artifact(
                        agent_id, attempt_id=attempt_id
                    )

                    async def remote_build():  # type: ignore[no-untyped-def]
                        if self._config.remote_build_mode == "off":
                            return None
                        # The remote and local caps are separate on purpose.
                        # A normal 70-minute lease budgets 25 minutes for
                        # Targon, then up to 45 minutes for local Docker. Do not
                        # derive this from the local cap: older hosts may carry
                        # a stale local override, which previously collapsed
                        # Targon to a one-minute attempt.
                        return await self._platform.build_submission_image(
                            agent_id,
                            attempt_id=attempt_id,
                            timeout=self._config.remote_build_timeout_seconds,
                        )

                    async def remote_build_consumed(build_id: UUID) -> None:
                        await self._platform.discard_submission_image_build(
                            agent_id,
                            attempt_id=attempt_id,
                            build_id=build_id,
                        )

                    async def remote_source_review() -> SourceReviewObservation | None:
                        payload = await self._platform.review_submission_source(
                            agent_id,
                            attempt_id=attempt_id,
                            timeout=self._config.source_review_timeout_seconds,
                        )
                        if payload is None:
                            return None
                        return SourceReviewObservation(
                            ok=payload.ok,
                            risk_level=payload.risk_level,
                            finding_digest=payload.finding_digest,
                            categories=tuple(payload.categories),
                            error_code=payload.error_code,
                            finding=(
                                payload.finding.model_dump(mode="json")
                                if payload.finding is not None
                                else None
                            ),
                            failure_disposition=payload.failure_disposition,
                            clearance_certified=payload.clearance_certified,
                            review_audit=(
                                payload.review_audit.model_dump(mode="json")
                                if payload.review_audit is not None
                                else None
                            ),
                        )

                    result = await self._gate.screen(
                        agent_id=agent_id,
                        attempt_id=attempt_id,
                        miner_hotkey=item.miner_hotkey,
                        sha256=item.sha256,
                        download_url=str(artifact.download_url),
                        progress=self._set_progress,
                        deadline=screen_deadline,
                        publish_image=publish_image,
                        remote_build=remote_build,
                        remote_build_consumed=remote_build_consumed,
                        remote_source_review=remote_source_review,
                        # A build-only item requests the mechanical lane. That
                        # lane is used both for an already-adjudicated rebuild
                        # and for score-first admission when the complete source
                        # and behavioral review is intentionally deferred. It
                        # builds, serves, isolates, and exports the image without
                        # spending private-policy/model budget.
                        build_only=item.build_only,
                        deferred_source_review=item.deferred_source_review,
                        policy_version=policy_version,
                    )
            shadow_review = self._gate.pop_shadow_review(attempt_id)
            if shadow_review is not None:
                await self._submit_shadow_review(
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    artifact_sha256=item.sha256.lower(),
                    result=shadow_review,
                )
            # Typed non-verdicts still complete and park the attempt. Reporting
            # removes the false "running" state; Platform requires an exact
            # operator override before it can be claimed again. During a
            # platform-first rolling deploy,
            # an older platform can reject this report safely: the worker logs
            # the failure and the legacy lease-expiry path remains authoritative.
            submits_result = result.submits_verdict or result.outcome in {
                ScreeningOutcome.QUARANTINE,
                ScreeningOutcome.INCONCLUSIVE,
                ScreeningOutcome.PASS_INCONCLUSIVE,
            }
            if not submits_result:
                logger.warning(
                    "screening agent_id=%s outcome=%s manifest=%s; "
                    "no public verdict submitted and lease remains authoritative",
                    agent_id,
                    result.outcome,
                    result.manifest_digest,
                )
                return
            self._set_progress("submitting")
            typed_outcome = ScreenResultOutcome(result.outcome.value)
            passed = typed_outcome in {
                ScreenResultOutcome.PASS,
                ScreenResultOutcome.PASS_INCONCLUSIVE,
            }
            if passed and (screened_image is None or screened_image_upload_id is None):
                raise PlatformError("passing screen did not publish a prebuilt image")
            is_quarantine = typed_outcome == ScreenResultOutcome.QUARANTINE
            is_audited_result = typed_outcome in {
                ScreenResultOutcome.QUARANTINE,
                ScreenResultOutcome.PASS_INCONCLUSIVE,
            }
            # The mechanical lane did not collect source-review evidence, so it
            # must never quarantine on that basis. The gate already guarantees
            # this; guard here too so a regression fails loudly.
            if item.build_only and not item.deferred_source_review and is_quarantine:
                raise PlatformError(
                    "build-only screen produced a quarantine outcome for "
                    f"agent_id={agent_id}"
                )
            reason_code = (
                "source-review-inconclusive"
                if typed_outcome == ScreenResultOutcome.PASS_INCONCLUSIVE
                else result.evidence[-1].code
                if result.evidence
                else None
            )
            # The bounded review payloads ride along on quarantine so the
            # operator sees WHY, not just a digest. When a source-review
            # finding exists, the signed finding_digest binds that finding;
            # otherwise it anchors the last module evidence digest as before.
            finding = (
                SourceReviewFinding.model_validate(result.finding)
                if is_audited_result and result.finding is not None
                else None
            )
            evidence = (
                [
                    ScreenEvidenceItem(
                        module_id=item.module_id,
                        code=item.code,
                        summary=item.summary,
                        digest=item.digest,
                    )
                    for item in result.evidence
                ]
                if is_audited_result and result.evidence
                else None
            )
            review_audit = (
                ScreenReviewAudit.model_validate(result.review_audit)
                if typed_outcome == ScreenResultOutcome.PASS_INCONCLUSIVE
                and result.review_audit is not None
                else None
            )
            review_audit_digest = (
                review_audit.canonical_digest() if review_audit is not None else None
            )
            finding_digest = (
                finding.canonical_digest()
                if finding is not None
                else next(
                    (item.digest for item in reversed(result.evidence) if item.digest),
                    None,
                )
                if is_audited_result
                else None
            )
            signature = sign_verdict(
                self._keypair,
                screener_hotkey=self._config.screener_hotkey,
                agent_id=agent_id,
                passed=passed,
                policy_version=policy_version,
                attempt_id=attempt_id,
                outcome=typed_outcome,
                manifest_digest=result.manifest_digest if is_audited_result else None,
                finding_digest=finding_digest,
                review_audit_digest=review_audit_digest,
                deferred_source_review=item.deferred_source_review,
                review_settings_revision=(
                    self._review_settings_status.revision
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                review_settings_instance_id=(
                    self._instance_id
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                review_settings_scope=(
                    self._review_settings_status.scope
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                review_settings_checksum=(
                    self._review_settings_status.checksum
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                reason_code=reason_code,
                image_sha256=screened_image.sha256 if screened_image else None,
                image_size_bytes=screened_image.size_bytes if screened_image else None,
                image_id=screened_image.image_id if screened_image else None,
                image_ref=screened_image.image_ref if screened_image else None,
                image_upload_id=screened_image_upload_id,
            )
            resp = await self._platform.submit_result(
                agent_id,
                signature=signature,
                passed=passed,
                policy_version=policy_version,
                detail=result.detail,
                attempt_id=attempt_id,
                outcome=typed_outcome,
                manifest_digest=result.manifest_digest if is_audited_result else None,
                finding_digest=finding_digest,
                review_audit_digest=review_audit_digest,
                review_settings_revision=(
                    self._review_settings_status.revision
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                review_settings_instance_id=(
                    self._instance_id
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                review_settings_scope=(
                    self._review_settings_status.scope
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                review_settings_checksum=(
                    self._review_settings_status.checksum
                    if self._review_settings_status.revision >= 1
                    else None
                ),
                reason_code=reason_code,
                evidence=evidence,
                finding=finding,
                review_audit=review_audit,
                image_sha256=screened_image.sha256 if screened_image else None,
                image_size_bytes=screened_image.size_bytes if screened_image else None,
                image_id=screened_image.image_id if screened_image else None,
                image_ref=screened_image.image_ref if screened_image else None,
                image_upload_id=screened_image_upload_id,
                build_only=item.build_only,
                deferred_source_review=item.deferred_source_review,
            )
            logger.info(
                "screened agent_id=%s miner=%s outcome=%s passed=%s "
                "elapsed_s=%d -> %s%s",
                agent_id,
                item.miner_hotkey,
                result.outcome,
                passed,
                int(time.time()) - (self._job_started_at or int(time.time())),
                resp.status,
                f" detail={result.detail!r}" if result.detail else "",
            )
        except PlatformError as e:
            # A late/conflicting verdict (409) or transient error: log + move on.
            logger.warning("verdict for agent_id=%s not applied: %s", agent_id, e)
        finally:
            # A review can finish before a later build/image step raises. Do not
            # retain that attempt's private result in the long-lived worker.
            self._gate.pop_shadow_review(attempt_id)
            heartbeat_stop.set()
            await heartbeat_task
            progress_tasks = tuple(self._progress_heartbeat_tasks)
            for task in progress_tasks:
                task.cancel()
            await asyncio.gather(*progress_tasks, return_exceptions=True)
            self._progress_heartbeat_tasks.clear()
            self._active_agent_id = None
            self._active_progress_stage = None
            self._active_lease_deadline = None
            self._job_started_at = None
            await self._report_heartbeat("polling", force=True)

    async def _submit_shadow_review(
        self,
        *,
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        result: L2RunResult,
    ) -> None:
        """Best-effort telemetry that can never change the signed verdict."""
        settings = self._review_settings_status
        if settings is None or settings.mode != "shadow" or settings.revision < 1:
            logger.warning(
                "discarding shadow result without an applied platform revision"
            )
            return
        observation = result.observation
        risk_level = cast(
            Literal["low", "medium", "high"] | None, observation.risk_level
        )
        disposition: Literal["safe", "violation", "inconclusive", "retryable_infra"] = (
            "safe"
            if observation.ok and observation.risk_level == "low"
            else "violation"
            if observation.ok
            else "inconclusive"
            if observation.failure_disposition == "inconclusive"
            else "retryable_infra"
        )
        request = ShadowReviewObservationRequest(
            attempt_id=attempt_id,
            artifact_sha256=artifact_sha256,
            settings_revision=settings.revision,
            settings_scope=settings.scope,
            settings_checksum=settings.checksum,
            disposition=disposition,
            risk_level=risk_level,
            categories=observation.categories,
            finding_digest=observation.finding_digest,
            resolution_basis=result.resolution_basis,
            clearance_path=result.clearance_path,
            critic_disposition=result.critic_disposition,
            adjudicator_disposition=result.adjudicator_disposition,
            response_models=result.response_models,
            response_providers=result.response_providers,
            usage=ShadowReviewUsage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
                estimated_cost_usd=result.usage.estimated_cost_usd,
                reported_cost_usd=result.usage.reported_cost_usd,
            ),
        )
        try:
            await self._platform.submit_shadow_review(agent_id, request)
        except PlatformError as error:
            logger.warning(
                "shadow review telemetry was not persisted attempt_id=%s: %s",
                attempt_id,
                error,
            )

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early if ``stop`` is set."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
