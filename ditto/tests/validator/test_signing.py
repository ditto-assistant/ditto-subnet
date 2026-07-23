"""Unit tests for the validator score signer.

Locks the canonical signing-message format, which the platform's
``_score_signing_message`` must reproduce byte-for-byte to verify.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import bittensor

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.benchmark_progress import BenchmarkProgress
from ditto.api_models.stack_health import (
    ValidatorComponentHealth,
    ValidatorStackHealth,
)
from ditto.api_models.system_health import DockerHealth, SystemMetrics
from ditto.api_models.validator import LedgerEntry, LedgerScoreProof
from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ValidatorCapabilities,
    ValidatorStackIdentity,
)
from ditto.validator.signing import (
    artifact_signing_message,
    heartbeat_signing_message,
    job_fail_signing_message,
    job_signing_message,
    ledger_signing_message,
    score_signing_message,
    sign_heartbeat,
    sign_job_fail_request,
    sign_job_request,
    sign_score,
    sign_top5_confirmation_job_request,
    top5_confirmation_job_signing_message,
    top5_confirmation_score_signing_message,
    verify_ledger_entry,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_DEADLINE = datetime(2026, 7, 9, 12, 30, tzinfo=UTC)
_V7_VECTOR = Path(__file__).parents[1] / "contract/validator_heartbeat_v7.json"
_V8_VECTOR = Path(__file__).parents[1] / "contract/validator_heartbeat_v8.json"
_V9_VECTOR = Path(__file__).parents[1] / "contract/validator_heartbeat_v9.json"


def _v9_request(
    fixture: dict,
) -> tuple[dict, ValidatorCapabilities, ValidatorStackIdentity, ValidatorStackHealth]:
    """Split one v9 vector request into kwargs + validated models."""
    request = dict(fixture["request"])
    raw_capabilities = request.pop("capabilities")
    raw_capabilities = {
        **raw_capabilities,
        "scorer_benchmarks": {
            **raw_capabilities["scorer_benchmarks"],
            "supported_bench_versions": tuple(
                raw_capabilities["scorer_benchmarks"]["supported_bench_versions"]
            ),
        },
    }
    capabilities = ValidatorCapabilities.model_validate(raw_capabilities)
    stack = ValidatorStackIdentity.model_validate(request.pop("stack"))
    stack_health = ValidatorStackHealth.model_validate(request.pop("stack_health"))
    return request, capabilities, stack, stack_health


def test_message_is_canonical_format() -> None:
    msg = score_signing_message(
        validator_hotkey=_HOTKEY,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=8675309,
    )
    assert (
        msg
        == (
            f"{_HOTKEY}:550e8400-e29b-41d4-a716-446655440000:"
            "2026-07-09T12:30:00.000000+00:00:run_1:0.82:8675309"
        ).encode()
    )


def test_top5_confirmation_score_binds_all_seed_composite_pairs() -> None:
    message = top5_confirmation_score_signing_message(
        validator_hotkey=_HOTKEY,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="confirmation-run",
        bench_version=6,
        confirmation_seeds=[11, 22],
        confirmation_composites=[0.7, 0.8],
    )
    assert (
        message
        == (
            "validator-top5-confirmation-score:v1:"
            f"{_HOTKEY}:{_AGENT}:2026-07-09T12:30:00.000000+00:00:"
            "confirmation-run:6:[[11,0.7],[22,0.8]]"
        ).encode()
    )


def test_transcript_digest_extends_canonical_format() -> None:
    # Offline reproducibility (v3 finding 3): a declared transcript digest is
    # appended to the canonical payload; absence keeps the legacy format so old
    # reports remain verifiable.
    digest = "cd" * 32
    base = score_signing_message(
        validator_hotkey=_HOTKEY,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=8675309,
    )
    extended = score_signing_message(
        validator_hotkey=_HOTKEY,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=8675309,
        transcript_sha256=digest,
    )
    assert extended == base + f":{digest}".encode()
    assert (
        score_signing_message(
            validator_hotkey=_HOTKEY,
            agent_id=_AGENT,
            ticket_deadline=_DEADLINE,
            run_id="run_1",
            composite=0.82,
            seed=8675309,
            transcript_sha256=None,
        )
        == base
    )


def test_swapped_transcript_digest_breaks_signature() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.50,
        seed=42,
        transcript_sha256="cd" * 32,
    )
    swapped = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.50,
        seed=42,
        transcript_sha256="ef" * 32,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert not verifier.verify(swapped, bytes.fromhex(sig_hex))


def test_sign_verifies_with_real_keypair() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=42,
    )
    msg = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=42,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert verifier.verify(msg, bytes.fromhex(sig_hex))


def test_tampered_composite_breaks_signature() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.50,
        seed=42,
    )
    # Verifying against a different composite must fail.
    tampered = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.99,
        seed=42,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert not verifier.verify(tampered, bytes.fromhex(sig_hex))


def test_superseded_ticket_deadline_breaks_signature() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.50,
        seed=42,
    )
    superseded = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE + timedelta(minutes=30),
        run_id="run_1",
        composite=0.50,
        seed=42,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert not verifier.verify(superseded, bytes.fromhex(sig_hex))


def test_job_claim_signature_binds_hotkey_nonce_and_timestamp() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    nonce = UUID("7f4d1800-4cf1-4a24-8fd5-2e4cd59942ae")
    requested_at = datetime(2026, 7, 14, 1, 30, tzinfo=UTC)
    signature = sign_job_request(
        keypair,
        validator_hotkey=keypair.ss58_address,
        nonce=nonce,
        requested_at=requested_at,
    )
    message = job_signing_message(
        validator_hotkey=keypair.ss58_address,
        nonce=nonce,
        requested_at=requested_at,
    )
    assert keypair.verify(message, bytes.fromhex(signature))
    replay_as_other_nonce = job_signing_message(
        validator_hotkey=keypair.ss58_address,
        nonce=UUID("e879178e-baf4-41f0-9467-9da18b65ac17"),
        requested_at=requested_at,
    )
    assert not keypair.verify(replay_as_other_nonce, bytes.fromhex(signature))


def test_job_fail_message_is_canonical_and_binds_the_exact_lease() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    ticket_deadline = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    nonce = UUID("7f4d1800-4cf1-4a24-8fd5-2e4cd59942ae")
    requested_at = datetime(2026, 7, 14, 1, 30, tzinfo=UTC)

    message = job_fail_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=agent_id,
        ticket_deadline=ticket_deadline,
        nonce=nonce,
        requested_at=requested_at,
    )
    assert (
        message
        == (
            f"validator-job-fail:v1:{keypair.ss58_address}:{agent_id}:"
            "2026-07-14T12:30:00.000000+00:00:"
            f"{nonce}:2026-07-14T01:30:00.000000+00:00"
        ).encode()
    )

    signature = sign_job_fail_request(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=agent_id,
        ticket_deadline=ticket_deadline,
        nonce=nonce,
        requested_at=requested_at,
    )
    assert keypair.verify(message, bytes.fromhex(signature))

    # A different lease deadline (a reissued ticket) must not verify: the fail
    # report can only close the exact lease it was signed for.
    other_deadline = job_fail_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=agent_id,
        ticket_deadline=datetime(2026, 7, 14, 13, 30, tzinfo=UTC),
        nonce=nonce,
        requested_at=requested_at,
    )
    assert not keypair.verify(other_deadline, bytes.fromhex(signature))


def test_top5_confirmation_claim_binds_champion_and_member() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    member = UUID("c9bf9e57-1685-4c89-bafb-ff5af830be8a")
    nonce = UUID("7f4d1800-4cf1-4a24-8fd5-2e4cd59942ae")
    requested_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    signature = sign_top5_confirmation_job_request(
        keypair,
        validator_hotkey=keypair.ss58_address,
        champion_agent_id=_AGENT,
        member_agent_id=member,
        nonce=nonce,
        requested_at=requested_at,
    )
    message = top5_confirmation_job_signing_message(
        validator_hotkey=keypair.ss58_address,
        champion_agent_id=_AGENT,
        member_agent_id=member,
        nonce=nonce,
        requested_at=requested_at,
    )
    assert keypair.verify(message, bytes.fromhex(signature))
    # A distinct domain tag from the single-leader claim, so a signature for one
    # lane cannot be replayed into the other, and champion/member are not swappable.
    assert message.startswith(b"validator-top5-confirmation-job:v1:")
    swapped = top5_confirmation_job_signing_message(
        validator_hotkey=keypair.ss58_address,
        champion_agent_id=member,
        member_agent_id=_AGENT,
        nonce=nonce,
        requested_at=requested_at,
    )
    assert not keypair.verify(swapped, bytes.fromhex(signature))


def test_artifact_signature_binds_agent_nonce_and_timestamp() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    nonce = UUID("7f4d1800-4cf1-4a24-8fd5-2e4cd59942ae")
    requested_at = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    message = artifact_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=agent_id,
        nonce=nonce,
        requested_at=requested_at,
    )
    signature = keypair.sign(message)

    assert keypair.verify(message, signature)
    other_agent = artifact_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=UUID("123e4567-e89b-12d3-a456-426614174000"),
        nonce=nonce,
        requested_at=requested_at,
    )
    assert not keypair.verify(other_agent, signature)


def test_ledger_signature_binds_nonce_and_timestamp() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    nonce = UUID("7f4d1800-4cf1-4a24-8fd5-2e4cd59942ae")
    requested_at = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    message = ledger_signing_message(
        validator_hotkey=keypair.ss58_address,
        nonce=nonce,
        requested_at=requested_at,
    )
    signature = keypair.sign(message)

    assert keypair.verify(message, signature)
    replay_as_other_nonce = ledger_signing_message(
        validator_hotkey=keypair.ss58_address,
        nonce=UUID("e879178e-baf4-41f0-9467-9da18b65ac17"),
        requested_at=requested_at,
    )
    assert not keypair.verify(replay_as_other_nonce, signature)


def test_heartbeat_signature_binds_build_state_and_timestamp() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    signature = sign_heartbeat(
        keypair,
        validator_hotkey=keypair.ss58_address,
        software_version="0.1.0",
        protocol_version=2,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        timestamp=1_752_443_200,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
    assert not verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="cd" * 32,
            state="running_benchmark",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
    assert not verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="ab" * 32,
            state="idle",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
    assert not verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )


def test_score_signature_adds_benchmark_version_only_for_v3() -> None:
    def message(bench_version: int | None = None) -> bytes:
        return score_signing_message(
            validator_hotkey=_HOTKEY,
            agent_id=_AGENT,
            run_id="run-1",
            composite=0.9,
            seed=42,
            bench_version=bench_version,
        )

    legacy = message()
    assert message(None) == legacy
    assert message(3) == legacy + b":3"


def test_score_signature_optional_suffix_order_golden() -> None:
    """Platform and validator append benchmark version before transcript hash."""
    digest = "cd" * 32
    base = (f"{_HOTKEY}:{_AGENT}::run-1:0.9:42").encode()
    cases = (
        (None, None, base),
        (3, None, base + b":3"),
        (None, digest, base + f":{digest}".encode()),
        (3, digest, base + f":3:{digest}".encode()),
    )
    for bench_version, transcript_sha256, expected in cases:
        assert (
            score_signing_message(
                validator_hotkey=_HOTKEY,
                agent_id=_AGENT,
                run_id="run-1",
                composite=0.9,
                seed=42,
                bench_version=bench_version,
                transcript_sha256=transcript_sha256,
            )
            == expected
        )


def test_protocol_v3_heartbeat_signature_binds_system_metrics() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    metrics = SystemMetrics(
        collected_at=1_752_443_200,
        cpu_percent=15,
        memory_percent=40,
        disk_percent=55,
        docker=DockerHealth(
            status="healthy", running_containers=4, unhealthy_containers=0
        ),
    )
    signature = sign_heartbeat(
        keypair,
        validator_hotkey=keypair.ss58_address,
        software_version="0.4.2",
        protocol_version=3,
        code_digest="ab" * 32,
        state="idle",
        system_metrics=metrics,
        timestamp=1_752_443_200,
    )
    tampered = metrics.model_copy(update={"memory_percent": 90})
    assert not keypair.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.4.2",
            protocol_version=3,
            code_digest="ab" * 32,
            state="idle",
            system_metrics=tampered,
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )


def test_protocol_v2_heartbeat_message_remains_backward_compatible() -> None:
    message = heartbeat_signing_message(
        validator_hotkey=_HOTKEY,
        software_version="0.4.2",
        protocol_version=2,
        code_digest="ab" * 32,
        state="idle",
        timestamp=1_752_443_200,
    )
    assert (
        message
        == (
            "ditto-validator-heartbeat:v2:"
            f"{_HOTKEY}:0.4.2:2:{'ab' * 32}:idle::1752443200"
        ).encode()
    )


def test_protocol_v1_heartbeat_message_remains_backward_compatible() -> None:
    message = heartbeat_signing_message(
        validator_hotkey=_HOTKEY,
        software_version="0.4.2",
        protocol_version=1,
        code_digest="ab" * 32,
        state="idle",
        timestamp=1_752_443_200,
    )
    assert (
        message
        == (
            "ditto-validator-heartbeat:v1:"
            f"{_HOTKEY}:0.4.2:1:{'ab' * 32}:idle:1752443200"
        ).encode()
    )


def test_protocol_v3_heartbeat_message_remains_backward_compatible() -> None:
    metrics = SystemMetrics(
        collected_at=1_752_443_200,
        cpu_percent=15,
        memory_percent=40,
        disk_percent=55,
        docker=DockerHealth(
            status="healthy", running_containers=4, unhealthy_containers=0
        ),
    )
    message = heartbeat_signing_message(
        validator_hotkey=_HOTKEY,
        software_version="0.4.2",
        protocol_version=3,
        code_digest="ab" * 32,
        state="idle",
        system_metrics=metrics,
        timestamp=1_752_443_200,
    )
    assert (
        message
        == (
            "ditto-validator-heartbeat:v3:"
            f"{_HOTKEY}:0.4.2:3:{'ab' * 32}:idle::"
            "1752443200,15,40,55,healthy,4,0:1752443200"
        ).encode()
    )


def test_protocol_v4_heartbeat_binds_progress_and_exact_ticket() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
    )
    signature = sign_heartbeat(
        keypair,
        validator_hotkey=keypair.ss58_address,
        software_version="0.4.2",
        protocol_version=4,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=_AGENT,
        benchmark_progress=progress,
        timestamp=1_752_443_200,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    original = heartbeat_signing_message(
        validator_hotkey=keypair.ss58_address,
        software_version="0.4.2",
        protocol_version=4,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=_AGENT,
        benchmark_progress=progress,
        timestamp=1_752_443_200,
    )
    assert verifier.verify(original, bytes.fromhex(signature))
    for tampered in (
        progress.model_copy(update={"completed": 57}),
        progress.model_copy(update={"stage": "failed_retrying"}),
        progress.model_copy(
            update={"ticket_deadline": _DEADLINE + timedelta(minutes=30)}
        ),
    ):
        message = heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.4.2",
            protocol_version=4,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=_AGENT,
            benchmark_progress=tampered,
            timestamp=1_752_443_200,
        )
        assert not verifier.verify(message, bytes.fromhex(signature))


def test_protocol_v4_heartbeat_canonical_vector() -> None:
    """Freeze the exact cross-repository protocol-v4 signature bytes."""
    agent_id = UUID("11111111-2222-4333-8444-555555555555")
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=datetime(2030, 1, 1, tzinfo=UTC),
    )
    actual = heartbeat_signing_message(
        validator_hotkey=_HOTKEY,
        software_version="1.2.3",
        protocol_version=4,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=agent_id,
        system_metrics=None,
        benchmark_progress=progress,
        timestamp=1_784_020_800,
    )
    expected = (
        b"ditto-validator-heartbeat:v4:"
        b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:"
        b"1.2.3:4:"
        b"abababababababababababababababababababababababababababababababab:"
        b"running_benchmark:11111111-2222-4333-8444-555555555555:-:"
        b"running_benchmark,51,114,2030-01-01T00:00:00.000000+00:00:"
        b"1784020800"
    )
    assert actual == expected

    keypair = bittensor.Keypair.create_from_uri("//Alice")
    signature = sign_heartbeat(
        keypair,
        validator_hotkey=_HOTKEY,
        software_version="1.2.3",
        protocol_version=4,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=agent_id,
        system_metrics=None,
        benchmark_progress=progress,
        timestamp=1_784_020_800,
    )
    assert keypair.verify(expected, bytes.fromhex(signature))


def test_protocol_v5_and_v6_messages_remain_backward_compatible() -> None:
    """v5/v6 changed updater coordination, not the signed heartbeat fields."""
    for protocol in (5, 6):
        actual = heartbeat_signing_message(
            validator_hotkey=_HOTKEY,
            software_version="1.2.3",
            protocol_version=protocol,
            code_digest="ab" * 32,
            state="idle",
            timestamp=1_784_020_800,
        )
        expected = (
            "ditto-validator-heartbeat:v4:"
            f"{_HOTKEY}:1.2.3:{protocol}:{'ab' * 32}:idle::-:-:1784020800"
        ).encode()
        assert actual == expected


def test_protocol_v7_heartbeat_cross_repository_vector() -> None:
    """Freeze the exact combined capability and stack signature bytes."""
    vector = json.loads(_V7_VECTOR.read_text())
    request = vector["request"]
    capabilities = ValidatorCapabilities.model_validate(request.pop("capabilities"))
    stack = ValidatorStackIdentity.model_validate(request.pop("stack"))

    actual = heartbeat_signing_message(
        **request, capabilities=capabilities, stack=stack
    )
    assert actual.decode() == vector["expected_message_utf8"]
    assert actual.hex() == vector["expected_message_hex"]


def test_protocol_v7_signature_binds_capabilities_and_stack() -> None:
    vector = json.loads(_V7_VECTOR.read_text())
    request = vector["request"]
    capabilities = ValidatorCapabilities.model_validate(request.pop("capabilities"))
    stack = ValidatorStackIdentity.model_validate(request.pop("stack"))
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    signature = sign_heartbeat(
        keypair, **request, capabilities=capabilities, stack=stack
    )
    tampered = capabilities.model_copy(update={"require_screened_image": True})
    message = heartbeat_signing_message(**request, capabilities=tampered, stack=stack)
    assert not keypair.verify(message, bytes.fromhex(signature))


def test_protocol_v8_binds_verified_scorer_benchmark_capability() -> None:
    vector = json.loads(_V7_VECTOR.read_text())
    v8_vector = json.loads(_V8_VECTOR.read_text())
    request = vector["request"] | {"protocol_version": 8}
    capabilities = ValidatorCapabilities.model_validate(request.pop("capabilities"))
    stack = ValidatorStackIdentity.model_validate(request.pop("stack"))
    capabilities = capabilities.model_copy(
        update={
            "scorer_benchmarks": ScorerBenchmarkCapability.model_validate(
                {
                    **v8_vector["scorer_benchmarks"],
                    "supported_bench_versions": tuple(
                        v8_vector["scorer_benchmarks"]["supported_bench_versions"]
                    ),
                }
            )
        }
    )
    message = heartbeat_signing_message(
        **request, capabilities=capabilities, stack=stack
    )
    assert message == v8_vector["expected_message_utf8"].encode()
    assert hashlib.sha256(message).hexdigest() == v8_vector["expected_message_sha256"]


def test_protocol_v9_heartbeat_cross_repository_vectors() -> None:
    """Freeze the exact v9 bytes for both the managed and the source stack."""
    vectors = json.loads(_V9_VECTOR.read_text())
    for name in ("managed", "source"):
        request, capabilities, stack, stack_health = _v9_request(vectors[name])
        message = heartbeat_signing_message(
            **request,
            capabilities=capabilities,
            stack=stack,
            stack_health=stack_health,
        )
        assert message.decode() == vectors[name]["expected_message_utf8"], name
        assert (
            hashlib.sha256(message).hexdigest()
            == vectors[name]["expected_message_sha256"]
        ), name


def test_protocol_v9_signature_binds_component_health() -> None:
    vectors = json.loads(_V9_VECTOR.read_text())
    request, capabilities, stack, stack_health = _v9_request(vectors["managed"])
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    signature = sign_heartbeat(
        keypair,
        **request,
        capabilities=capabilities,
        stack=stack,
        stack_health=stack_health,
    )
    # Upgrading one component's reported state must break the signature.
    tampered = stack_health.model_copy(
        update={
            "ollama": ValidatorComponentHealth(
                health="healthy",
                required=True,
                observed_at=1_784_020_740,
                ready=True,
                model_ready=True,
            )
        }
    )
    message = heartbeat_signing_message(
        **request,
        capabilities=capabilities,
        stack=stack,
        stack_health=tampered,
    )
    assert not keypair.verify(message, bytes.fromhex(signature))


def test_protocol_v9_requires_stack_health_and_v8_rejects_it() -> None:
    vectors = json.loads(_V9_VECTOR.read_text())
    request, capabilities, stack, stack_health = _v9_request(vectors["managed"])
    try:
        heartbeat_signing_message(**request, capabilities=capabilities, stack=stack)
        raise AssertionError("v9 without stack health must not sign")
    except ValueError as e:
        assert "stack health" in str(e)
    downgraded = request | {"protocol_version": 8}
    try:
        heartbeat_signing_message(
            **downgraded,
            capabilities=capabilities,
            stack=stack,
            stack_health=stack_health,
        )
        raise AssertionError("v8 with stack health must not sign")
    except ValueError as e:
        assert "v9" in str(e)


def test_protocol_v10_heartbeat_binds_all_slot_capacity() -> None:
    """Freeze the additive v10 suffix shared with the platform verifier."""
    vectors = json.loads(_V9_VECTOR.read_text())
    request, capabilities, stack, stack_health = _v9_request(vectors["managed"])
    request["protocol_version"] = 10
    capacity = BenchmarkCapacity(configured_slots=2, healthy_slots=["slot-0", "slot-1"])
    message = heartbeat_signing_message(
        **request,
        capabilities=capabilities,
        stack=stack,
        stack_health=stack_health,
        benchmark_capacity=capacity,
    )
    assert message.endswith(
        b':94:{"active":[],"admission":"accepting","configured_slots":2,'
        b'"healthy_slots":["slot-0","slot-1"]}:1784020800'
    )
    assert hashlib.sha256(message).hexdigest() == (
        "036a7cd3e541ab381635e6c497f4a788f86198ffa713dac205c052aec48f9d53"
    )

    try:
        heartbeat_signing_message(
            **(request | {"protocol_version": 9}),
            capabilities=capabilities,
            stack=stack,
            stack_health=stack_health,
            benchmark_capacity=capacity,
        )
        raise AssertionError("v9 must reject an unsigned capacity extension")
    except ValueError as error:
        assert "v10" in str(error)


def test_protocol_v11_heartbeat_uses_the_platform_v11_domain() -> None:
    """Keep protocol 11 signatures in the platform verifier's domain."""
    vectors = json.loads(_V9_VECTOR.read_text())
    request, capabilities, stack, stack_health = _v9_request(vectors["managed"])
    request["protocol_version"] = 11
    capacity = BenchmarkCapacity(configured_slots=2, healthy_slots=["slot-0", "slot-1"])

    message = heartbeat_signing_message(
        **request,
        capabilities=capabilities,
        stack=stack,
        stack_health=stack_health,
        benchmark_capacity=capacity,
    )

    assert message.startswith(b"ditto-validator-heartbeat:v11:")
    assert b":11:" in message


def _signed_ledger_entry(*, tamper_median: bool = False) -> LedgerEntry:
    keypairs = [bittensor.Keypair.create_from_uri(f"//Ledger{i}") for i in range(3)]
    composites = [0.4, 0.6, 0.8]
    proofs: list[LedgerScoreProof] = []
    for i, (keypair, composite) in enumerate(zip(keypairs, composites, strict=True)):
        run_id = f"run_{i}"
        signature = sign_score(
            keypair,
            validator_hotkey=keypair.ss58_address,
            agent_id=_AGENT,
            ticket_deadline=_DEADLINE,
            run_id=run_id,
            composite=composite,
            seed=i,
            bench_version=7,
            transcript_sha256="cd" * 32,
        )
        proofs.append(
            LedgerScoreProof(
                validator_hotkey=keypair.ss58_address,
                run_id=run_id,
                composite=composite,
                seed=i,
                bench_version=7,
                ticket_deadline=_DEADLINE,
                transcript_sha256="cd" * 32,
                signature=signature,
            )
        )
    median = proofs[1]
    return LedgerEntry(
        miner_hotkey=_HOTKEY,
        agent_id=_AGENT,
        composite=0.61 if tamper_median else median.composite,
        n=114,
        first_seen=_DEADLINE,
        sha256="ab" * 32,
        size_bytes=1024,
        run_id=median.run_id,
        seed=median.seed,
        validator_hotkey=median.validator_hotkey,
        bench_version=7,
        signature=median.signature,
        score_proofs=proofs,
        status=AgentStatus.SCORED,
    )


def test_ledger_entry_verifies_all_receipts_and_recomputed_median() -> None:
    assert verify_ledger_entry(_signed_ledger_entry())


def test_ledger_entry_rejects_platform_median_tampering() -> None:
    assert not verify_ledger_entry(_signed_ledger_entry(tamper_median=True))


def test_ledger_entry_rejects_tampered_receipt() -> None:
    entry = _signed_ledger_entry()
    proofs = list(entry.score_proofs)
    proofs[0] = proofs[0].model_copy(update={"composite": 0.41})
    assert not verify_ledger_entry(entry.model_copy(update={"score_proofs": proofs}))


def test_legacy_unsigned_ledger_entry_remains_valid() -> None:
    entry = _signed_ledger_entry().model_copy(
        update={"bench_version": 6, "score_proofs": [], "signature": None}
    )
    assert verify_ledger_entry(entry)


def test_v7_ledger_entry_requires_signed_quorum() -> None:
    entry = _signed_ledger_entry().model_copy(update={"score_proofs": []})
    assert not verify_ledger_entry(entry)
