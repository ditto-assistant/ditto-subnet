"""Execute one replay-bound sealed V13 target and matched clean control.

Only trusted registry adapters and a protected read-only bank may supply the
inputs. The result is aggregate evidence, not a private policy finding.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ditto_screener.config import ScreenerConfig
from ditto_screener.platform import PlatformClient
from ditto_screener.v13_private_adapter import BoundFreshCaseExecutor, SealedDigestStore
from ditto_screener.v13_private_runtime import ConversationPrivateCaseSessionFactory
from ditto_screener.v13_replay_private_registry import PlatformReplayPrivateRegistry
from ditto_screening_protocol.v13_private_clean_control import (
    TrustedGenerationRegistry,
    TrustedGroupedPrivatePackageRegistry,
    TrustedKnownBenignRegistry,
    V13MatchedCleanControlCommitment,
    prepare_v13_matched_clean_control,
)
from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivateExecutionUnavailable,
    execute_v13_private_pairs,
)
from ditto_screening_protocol.v13_private_package import (
    ArtifactCommitment,
    SealedPackageStore,
    _prepare_registered_package,
)
from ditto_screening_protocol.v13_private_receipt import V13ReplayPrivateReceipt
from ditto_screening_protocol.v13_replay_observation import V13ReplayBinding


class V13ReplayPrivateExecution(BaseModel):
    """Sanitized output from actual sealed target and control execution."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    matched: V13MatchedCleanControlCommitment
    target: PrivateExecutionResult
    known_benign: PrivateExecutionResult


def _protected_bank(root: Path) -> SealedDigestStore:
    """Require a genuine read-only mount, not a worker-writable local folder."""
    try:
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or root.is_symlink()
            or not os.statvfs(root).f_flag & os.ST_RDONLY
        ):
            raise PrivateExecutionUnavailable("protected private bank unavailable")
    except OSError:
        raise PrivateExecutionUnavailable(
            "protected private bank unavailable"
        ) from None
    return SealedDigestStore(root)


async def run_registered_replay_private_group(
    *,
    replay_id: UUID,
    group_id: UUID,
    bank_root: Path,
    config: ScreenerConfig,
    provider_key: str,
    platform: PlatformClient,
    keypair: Any,
) -> dict[str, Any]:
    """Run and report the protected cases for an already claimed replay.

    No terminal action occurs here. The caller must first obtain an independent
    lease and provision a reviewed, read-only hidden bank outside this repo.
    The lease is renewed while cases execute; a failed renewal cancels work.
    """
    store = _protected_bank(bank_root)
    registry = PlatformReplayPrivateRegistry(
        platform, replay_id=replay_id, group_id=group_id
    )
    initial = await registry.snapshot()
    sessions = ConversationPrivateCaseSessionFactory(
        config=config,
        provider_key=provider_key,
        resolver=registry,
    )
    running = asyncio.current_task()
    if running is None:
        raise PrivateExecutionUnavailable("private runner task unavailable")

    async def renew_until_done() -> None:
        deadline = initial.lease_deadline
        while True:
            await asyncio.sleep(
                max(1.0, (deadline - datetime.now(UTC)).total_seconds() - 300)
            )
            try:
                renewed = await platform.renew_verification_replay(replay_id)
                deadline = datetime.fromisoformat(
                    renewed["lease_deadline"].replace("Z", "+00:00")
                )
                if deadline.tzinfo is None or deadline <= datetime.now(UTC):
                    raise PrivateExecutionUnavailable("private lease renewal invalid")
            except Exception:
                running.cancel()
                return

    renewer = asyncio.create_task(renew_until_done())
    try:
        execution = await execute_replay_private_group(
            replay_id=replay_id,
            group_id=group_id,
            expected_pair_inventory_sha256=initial.pair_inventory_sha256,
            target=initial.target_image.commitment(),
            control=initial.control_image.commitment(),
            store=store,
            packages=registry,
            controls=registry,
            generations=registry,
            sessions=sessions,
            runner_hotkey=config.screener_hotkey,
        )
        # Obtain the current exact image ID and registry binding again before
        # signing. An expired or changed lease fails closed at this read.
        final = await registry.snapshot()
        if (
            final.group != initial.group
            or final.pair_inventory_sha256 != initial.pair_inventory_sha256
            or final.target_image.commitment() != initial.target_image.commitment()
            or final.control_image.commitment() != initial.control_image.commitment()
        ):
            raise PrivateExecutionUnavailable("private report binding changed")
        receipt = sign_replay_private_execution(
            execution=execution,
            binding=V13ReplayBinding(
                replay_id=replay_id,
                agent_id=final.target_image.agent_id,
                attempt_id=final.target_image.attempt_id,
                artifact_sha256=final.target_image.artifact_sha256,
                image_sha256=final.target_image.image_sha256,
                image_id=final.target_image.image_id,
            ),
            runner_hotkey=config.screener_hotkey,
            keypair=keypair,
        )
        return await platform.submit_replay_private_receipt(receipt)
    finally:
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer


def sign_replay_private_execution(
    *,
    execution: V13ReplayPrivateExecution,
    binding: V13ReplayBinding,
    runner_hotkey: str,
    keypair: Any,
) -> V13ReplayPrivateReceipt:
    """Sign only the canonical aggregate from completed protected execution."""
    if (
        execution.matched.replay_id != binding.replay_id
        or getattr(keypair, "ss58_address", None) != runner_hotkey
    ):
        raise PrivateExecutionUnavailable("private signer identity unavailable")
    unsigned = V13ReplayPrivateReceipt(
        binding=binding,
        matched=execution.matched,
        target=execution.target,
        known_benign=execution.known_benign,
        runner_hotkey=runner_hotkey,
        observed_at=datetime.now(UTC),
        signature="0" * 128,
    )
    signature = keypair.sign(unsigned.signing_message()).hex()
    return V13ReplayPrivateReceipt.model_validate(
        {**unsigned.model_dump(mode="json"), "signature": signature}
    )


async def execute_replay_private_group(
    *,
    replay_id: UUID,
    group_id: UUID,
    expected_pair_inventory_sha256: str,
    target: ArtifactCommitment,
    control: ArtifactCommitment,
    store: SealedPackageStore,
    packages: TrustedGroupedPrivatePackageRegistry,
    controls: TrustedKnownBenignRegistry,
    generations: TrustedGenerationRegistry,
    sessions: ConversationPrivateCaseSessionFactory,
    runner_hotkey: str,
) -> V13ReplayPrivateExecution:
    """Fail closed unless both exact images complete the same sealed inventory."""
    try:
        async with asyncio.timeout(7200):
            matched = await prepare_v13_matched_clean_control(
                replay_id=replay_id,
                group_id=group_id,
                target=target,
                control=control,
                store=store,
                packages=packages,
                controls=controls,
                generations=generations,
            )
            if matched.pair_inventory_sha256 != expected_pair_inventory_sha256:
                raise PrivateExecutionUnavailable("private registry inventory mismatch")
            target_registration = await packages.get_group_registration(
                group_id, "target"
            )
            control_registration = await packages.get_group_registration(
                group_id, "known_benign"
            )
            target_prepared = await _prepare_registered_package(
                store=store, commitment=target, registration=target_registration
            )
            control_prepared = await _prepare_registered_package(
                store=store, commitment=control, registration=control_registration
            )
            if (
                target_registration.manifest_sha256 != matched.target_manifest_sha256
                or control_registration.manifest_sha256
                != matched.control_manifest_sha256
                or target_prepared.manifest.pairs != control_prepared.manifest.pairs
                or len(target_prepared.manifest.pairs) != matched.pair_count
            ):
                raise PrivateExecutionUnavailable("private inventory changed")
            target_result = await execute_v13_private_pairs(
                prepared=target_prepared,
                store=store,
                executor=BoundFreshCaseExecutor(
                    role="target",
                    agent_id=target.agent_id,
                    attempt_id=target.attempt_id,
                    artifact_sha256=target.artifact_sha256,
                    image_sha256=target.image_sha256,
                    factory=sessions,
                ),
                runner_hotkey=runner_hotkey,
            )
            control_result = await execute_v13_private_pairs(
                prepared=control_prepared,
                store=store,
                executor=BoundFreshCaseExecutor(
                    role="known_benign",
                    agent_id=control.agent_id,
                    attempt_id=control.attempt_id,
                    artifact_sha256=control.artifact_sha256,
                    image_sha256=control.image_sha256,
                    factory=sessions,
                ),
                runner_hotkey=runner_hotkey,
            )
            if (
                target_result.summary.completed_pairs != matched.pair_count
                or control_result.summary.completed_pairs != matched.pair_count
                or sum(item.pairs for item in target_result.aggregates)
                != matched.pair_count
                or sum(item.pairs for item in control_result.aggregates)
                != matched.pair_count
                or [
                    (item.transformation_class, item.seed_commitment, item.pairs)
                    for item in target_result.aggregates
                ]
                != [
                    (item.transformation_class, item.seed_commitment, item.pairs)
                    for item in control_result.aggregates
                ]
            ):
                raise PrivateExecutionUnavailable("private execution pair mismatch")
            return V13ReplayPrivateExecution(
                matched=matched,
                target=target_result,
                known_benign=control_result,
            )
    except PrivateExecutionUnavailable:
        raise
    except Exception:
        raise PrivateExecutionUnavailable(
            "replay private execution unavailable"
        ) from None
