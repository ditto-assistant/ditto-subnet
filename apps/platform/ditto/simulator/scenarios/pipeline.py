"""Every in-flight submission stage visible at once.

Populates the full lifecycle so ``/public/operations`` and ``/public/activity``
render every lane simultaneously: uploads waiting for screening, one agent
mid-screening with live screener progress, evaluation at each interesting
sub-state (0-, 1-, and 2-of-3 scores including the provisional-contender lane,
plus three live benchmark runs at ~15%, ~60%, and the 95%-capped finalizing
stage), a below-score-floor drop, quarantine/reject outcomes (one
dispute-eligible), and a live ``collecting`` benchmark v3 rollout with one
v3-capable validator.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import (
    BenchmarkRollout,
    BenchmarkRolloutMember,
    ScreeningAttempt,
    ScreeningQuarantineResolution,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.simulator.fabric import Fabric
    from ditto.simulator.scenarios import ScenarioContext

NAME = "pipeline"
DESCRIPTION = (
    "Every lifecycle stage at once: screening, all evaluating sub-states, "
    "live benchmark progress, quarantine/reject, and a collecting v3 rollout."
)

# Composites for the finalized field. Five-plus eligible agents make the
# score-continuation floor real (the fifth-place composite), which the
# below-floor lane needs to render.
_FINALIZED_COMPOSITES = (0.82, 0.78, 0.74, 0.70, 0.66, 0.62)


def _benchmark_progress(
    *, stage: str, completed: int, total: int, deadline_iso: str
) -> dict[str, Any]:
    """A signed-progress payload bound to the exact issued-ticket deadline."""
    return {
        "stage": stage,
        "completed": completed,
        "total": total,
        "ticket_deadline": deadline_iso,
    }


async def _running_benchmark(
    session: AsyncSession,
    f: Fabric,
    *,
    index: int,
    validator_name: str,
    stage: str,
    completed: int,
    total: int,
) -> None:
    """One evaluating agent with a live issued ticket + synchronized heartbeat.

    The fabric issues the ticket with ``deadline = minutes_from_now(50)`` for
    the first ``issued_to`` name; the signed progress must carry that exact
    deadline or the public projection drops it as a stale lease.
    """
    agent = await f.evaluating_agent(session, index=index, issued_to=[validator_name])
    deadline = f.minutes_from_now(50)
    await f.validator_heartbeat(
        session,
        name=validator_name,
        state="running_benchmark",
        active_agent_id=agent.agent_id,
        benchmark_progress=_benchmark_progress(
            stage=stage,
            completed=completed,
            total=total,
            deadline_iso=deadline.isoformat(),
        ),
    )


async def apply(ctx: ScenarioContext) -> None:
    f = ctx.fabric
    async with ctx.session_maker() as session, session.begin():
        # ── fleet: three scoring validators online + one v3-capable canary ──
        for name in ("validator-1", "validator-2", "validator-3"):
            await f.validator_heartbeat(session, name=name)
        scorer_revision = f.hex_digest("dittobench-api-revision")[:40]
        await f.validator_heartbeat(
            session,
            name="validator-7",
            protocol_version=8,
            capabilities={
                "screened_images": True,
                "require_screened_image": True,
                "source_build_fallback": False,
                "full_stack_managed": True,
                "stack_updater": True,
                "sandbox_egress_restricted": True,
                "executor_isolation": "ephemeral_vm",
                "scorer_benchmarks": {
                    "status": "fresh_verified",
                    "supported_bench_versions": [2, 3],
                    "observed_at": int(f.now.timestamp()),
                    "software_version": "0.9.0",
                    "source_revision": scorer_revision,
                },
            },
            stack={
                "mode": "managed",
                "compose_schema": 2,
                "release_descriptor_digest": (
                    "sha256:" + f.hex_digest("release-descriptor")
                ),
                "components": {
                    component: {
                        "image_digest": (
                            "sha256:" + f.hex_digest(f"component:{component}")
                        ),
                        "provenance": "signed_descriptor",
                        **(
                            {
                                "source_revision": scorer_revision,
                                "version": "0.9.0",
                            }
                            if component == "dittobench_api"
                            else {}
                        ),
                    }
                    for component in (
                        "ditto_subnet",
                        "dittobench_api",
                        "sandbox_docker",
                        "model_relay",
                        "pylon",
                        "ollama",
                    )
                },
            },
        )

        # ── finalized field (indices 1-6): makes the score floor exist ──────
        finalized = [
            await f.finalized_agent(session, index=i, composite=composite)
            for i, composite in enumerate(_FINALIZED_COMPOSITES, start=1)
        ]

        # ── below the score floor: 2-of-3 scores, no live ticket, sub-floor ─
        await f.evaluating_agent(
            session,
            index=7,
            scored_by=["validator-1", "validator-2"],
            composite=0.31,
        )

        # ── waiting for screening ───────────────────────────────────────────
        await f.uploaded_agent(session, index=8)
        await f.uploaded_agent(session, index=9)

        # ── mid-screening with live screener progress ───────────────────────
        screening, attempt = await f.screening_agent(session, index=10)
        await f.screener_heartbeat(
            session,
            name="screener-1",
            state="screening",
            active_agent_id=screening.agent_id,
            screening_progress={
                "stage": "source_review_50",
                "started_at": int(attempt.started_at.timestamp()),
            },
        )

        # ── evaluating sub-states ───────────────────────────────────────────
        # No scores yet: plain waiting_validator.
        await f.evaluating_agent(session, index=11)
        # 1-of-3 provisional.
        await f.evaluating_agent(
            session, index=12, scored_by=["validator-1"], composite=0.69
        )
        # 2-of-3 provisional contender (strong composite -> contender lane).
        await f.evaluating_agent(
            session,
            index=13,
            scored_by=["validator-2", "validator-3"],
            composite=0.79,
        )
        # Live benchmark runs: ~15%, ~60%, and the 95%-capped finalizing stage.
        await _running_benchmark(
            session,
            f,
            index=14,
            validator_name="validator-4",
            stage="running_benchmark",
            completed=18,
            total=120,
        )
        await _running_benchmark(
            session,
            f,
            index=15,
            validator_name="validator-5",
            stage="running_benchmark",
            completed=72,
            total=120,
        )
        await _running_benchmark(
            session,
            f,
            index=16,
            validator_name="validator-6",
            stage="finalizing",
            completed=120,
            total=120,
        )

        # ── screening_failed: infra flake, will be retried ──────────────────
        flaky = await f.uploaded_agent(session, index=17)
        flaky.status = AgentStatus.SCREENING_FAILED
        failed_started = f.hours_ago(2)
        session.add(
            ScreeningAttempt(
                attempt_id=f.uuid(f"attempt:{flaky.agent_id}"),
                agent_id=flaky.agent_id,
                screener_hotkey=f.ss58_hotkey("screener:screener-1"),
                policy_version=flaky.screening_policy_version,
                status="failed",
                started_at=failed_started,
                deadline=failed_started + timedelta(hours=1),
                finished_at=failed_started + timedelta(minutes=9),
            )
        )
        await session.flush()

        # ── active quarantine with evidence + source-review finding ─────────
        _, quarantine = await f.quarantined_agent(
            session, index=18, reason_code="policy-dynamic-exec"
        )
        quarantine.finding_digest = f.hex_digest(f"finding:{quarantine.agent_id}")
        quarantine.finding = {
            "artifact_sha256": f.hex_digest(f"tarball-finding:{quarantine.agent_id}"),
            "prompt_revision": "sr-2026-06",
            "risk_level": "high",
            "confidence": 0.92,
            "categories": ["dynamic-exec"],
            "evidence": [
                {"path": "src/agent.rs", "line": 118, "category": "dynamic-exec"}
            ],
            "summary": (
                "agent decodes and executes a base64 payload at runtime, "
                "bypassing the static import allowlist"
            ),
        }

        # ── rejected via resolved-reject quarantine (dispute-eligible) ──────
        rejected, reject_quarantine = await f.quarantined_agent(
            session, index=19, reason_code="policy-network-exfil"
        )
        rejected.status = AgentStatus.REJECTED
        rejected.screening_reason = "quarantine resolved: network exfiltration attempt"
        rejected.screening_reason_code = "policy-network-exfil"
        resolved_at = f.hours_ago(3)
        reject_quarantine.status = "resolved"
        reject_quarantine.resolution = "reject"
        reject_quarantine.resolved_at = resolved_at
        reject_quarantine.resolved_by = "operator:sim"
        reject_quarantine.resolution_reason = (
            "confirmed exfiltration of dataset contents to an external host"
        )
        session.add(
            ScreeningQuarantineResolution(
                resolution_id=f.uuid(f"resolution:{reject_quarantine.quarantine_id}"),
                quarantine_id=reject_quarantine.quarantine_id,
                resolution="reject",
                reason=reject_quarantine.resolution_reason,
                actor="operator:sim",
                created_at=resolved_at,
            )
        )
        await session.flush()

        # ── plain deterministic screening rejection ─────────────────────────
        await f.rejected_agent(session, index=20)

        # ── collecting v2 -> v3 rollout with three frozen cohort members ────
        rollout = BenchmarkRollout(
            rollout_id=f.uuid("rollout:2->3"),
            from_version=2,
            desired_version=3,
            status="collecting",
            cohort_size=5,
            created_at=f.hours_ago(4),
        )
        session.add(rollout)
        await session.flush()
        session.add_all(
            [
                BenchmarkRolloutMember(
                    rollout_id=rollout.rollout_id,
                    agent_id=agent.agent_id,
                    position=position,
                    frozen_miner_hotkey=agent.miner_hotkey,
                    frozen_composite=composite,
                )
                for position, (agent, composite) in enumerate(
                    zip(finalized[:3], _FINALIZED_COMPOSITES[:3], strict=True),
                    start=1,
                )
            ]
        )
        await session.flush()
