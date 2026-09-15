from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    HostedCodingStatus,
    hosted_message_digest,
    hosted_signing_bytes,
)
from ditto.tests.validator.test_coding_hosted_transport import Chunks
from ditto.validator import coding_hosted_control as control
from ditto.validator.coding_hosted_transport import HostedCodingTransportError

NOW = 1788590000
PLATFORM = bittensor.Keypair.create_from_uri("//Alice")
VALIDATOR = bittensor.Keypair.create_from_uri("//Bob")
# Pinned in apps/platform/ditto/tests/db/queries/test_coding_hosted_assignment_vector.py
# against HostedAssignmentAuthority.digest(); both sides must agree.
GOLDEN_SHA256 = "3dbb4daee1b6563f57aece30bfcf54a5a146a9d641fc6c6f263d4f4d1ef5ec8e"


def projection() -> dict[str, Any]:
    return {
        "schema": "dittobench-coding-hosted-assignment-v2",
        "coding_contract_version": 2,
        "shadow_only": True,
        "weight_eligible": False,
        "evaluation_id": "10000000-0000-4000-8000-000000000001",
        "attempt_id": "20000000-0000-4000-8000-000000000002",
        "release_row_id": "30000000-0000-4000-8000-000000000003",
        "registration_sha256": "a" * 64,
        "agent_id": "40000000-0000-4000-8000-000000000004",
        "validator_hotkey": VALIDATOR.ss58_address,
        "artifact_sha256": "2" * 64,
        "screened_image_sha256": "b" * 64,
        "selection_sha256": "c" * 64,
        "policy_sha256": "4" * 64,
        "execution_profile_sha256": "5" * 64,
        "grading_profile_sha256": "6" * 64,
        "deadline_unix": NOW + 1800,
    }


def write_assignment(tmp_path: Path, value: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(projection() if value is None else value))
    return path


class Signer:
    """Exposes only what the command may touch on the in-place keypair."""

    def __init__(self) -> None:
        self.ss58_address = VALIDATOR.ss58_address
        self.signed = 0

    def sign(self, data: bytes) -> bytes:
        self.signed += 1
        return VALIDATOR.sign(data)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"command touched keypair attribute {name}")


def context(signer: Signer | None = None) -> control.HostedControlContext:
    return control.HostedControlContext(
        platform_origin="https://platform.example",
        platform_hotkey=PLATFORM.ss58_address,
        keypair=signer or Signer(),
        verifier=bittensor.Keypair(ss58_address=PLATFORM.ss58_address),
    )


def test_assignment_digest_matches_the_platform_projection_vector() -> None:
    assignment = projection()
    assignment["deadline_unix"] = 1788593600
    assert control.assignment_sha256(assignment) == GOLDEN_SHA256


def test_assignment_file_is_bound_to_the_pinned_digest(tmp_path: Path) -> None:
    path = write_assignment(tmp_path)
    digest = control.assignment_sha256(projection())
    loaded = control.load_assignment(path, digest)
    assert loaded.assignment_sha256 == digest
    assert loaded.evaluation_id == UUID("10000000-0000-4000-8000-000000000001")
    with pytest.raises(control.HostedControlCommandError):
        control.load_assignment(path, "f" * 64)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(weight_eligible=True),
        lambda v: v.update(shadow_only=False),
        lambda v: v.update(coding_contract_version=True),
        lambda v: v.update(deadline_unix=True),
        lambda v: v.update(evaluation_id="10000000-0000-4000-8000-00000000000A"),
        lambda v: v.update(attempt_id="00000000-0000-0000-0000-000000000000"),
        lambda v: v.update(policy_sha256="G" * 64),
        lambda v: v.update(extra="PRIVATE_MARKER"),
        lambda v: v.pop("grading_profile_sha256"),
    ],
)
def test_assignment_file_rejects_drift_before_any_signing(
    tmp_path: Path, mutate: Any
) -> None:
    value = projection()
    mutate(value)
    path = write_assignment(tmp_path, value)
    pin = control.assignment_sha256(projection())
    with pytest.raises(control.HostedControlCommandError) as caught:
        control.load_assignment(path, pin)
    assert "PRIVATE_MARKER" not in str(caught.value)


def test_assignment_file_rejects_duplicate_keys_and_links(tmp_path: Path) -> None:
    pin = control.assignment_sha256(projection())
    body = json.dumps(projection())
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(body[:-1] + ', "deadline_unix": 1}')
    with pytest.raises(control.HostedControlCommandError):
        control.load_assignment(duplicate, pin)
    target = write_assignment(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(control.HostedControlCommandError):
        control.load_assignment(link, pin)


def _config(hotkey: str = VALIDATOR.ss58_address) -> SimpleNamespace:
    return SimpleNamespace(
        validator_hotkey=hotkey, platform_api_url="https://platform.example"
    )


@pytest.mark.parametrize(
    "environ,pinned,config,message",
    [
        ({}, VALIDATOR.ss58_address, _config(), "disabled"),
        (
            {control.ENABLED_ENV: "false"},
            VALIDATOR.ss58_address,
            _config(),
            "disabled",
        ),
        ({control.ENABLED_ENV: "true"}, VALIDATOR.ss58_address, _config(), "signer"),
        (
            {
                control.ENABLED_ENV: "true",
                control.PLATFORM_HOTKEY_ENV: VALIDATOR.ss58_address,
            },
            VALIDATOR.ss58_address,
            _config(),
            "signer",
        ),
        (
            {
                control.ENABLED_ENV: "true",
                control.PLATFORM_HOTKEY_ENV: PLATFORM.ss58_address,
            },
            PLATFORM.ss58_address,
            _config(),
            "pinned",
        ),
        (
            {
                control.ENABLED_ENV: "true",
                control.PLATFORM_HOTKEY_ENV: PLATFORM.ss58_address,
            },
            VALIDATOR.ss58_address,
            _config(PLATFORM.ss58_address),
            "pinned",
        ),
    ],
)
def test_context_is_default_off_and_bound_to_the_pinned_validator(
    environ: dict[str, str], pinned: str, config: SimpleNamespace, message: str
) -> None:
    with pytest.raises(control.HostedControlCommandError) as caught:
        control.load_context(
            environ,
            pinned_validator_hotkey=pinned,
            load_config=lambda: config,
            load_keypair=lambda _: Signer(),
            make_verifier=lambda address: bittensor.Keypair(ss58_address=address),
        )
    assert message in str(caught.value)


def test_context_refuses_a_loaded_key_that_differs_from_the_pin() -> None:
    other = Signer()
    other.ss58_address = PLATFORM.ss58_address
    with pytest.raises(control.HostedControlCommandError) as caught:
        control.load_context(
            {
                control.ENABLED_ENV: "true",
                control.PLATFORM_HOTKEY_ENV: bittensor.Keypair.create_from_uri(
                    "//Charlie"
                ).ss58_address,
            },
            pinned_validator_hotkey=VALIDATOR.ss58_address,
            load_config=_config,
            load_keypair=lambda _: other,
            make_verifier=lambda address: bittensor.Keypair(ss58_address=address),
        )
    assert "pinned" in str(caught.value)


def test_context_trusts_only_the_configured_platform_signer() -> None:
    loaded = control.load_context(
        {
            control.ENABLED_ENV: "true",
            control.PLATFORM_HOTKEY_ENV: PLATFORM.ss58_address,
        },
        pinned_validator_hotkey=VALIDATOR.ss58_address,
        load_config=_config,
        load_keypair=lambda _: Signer(),
        make_verifier=lambda address: bittensor.Keypair(ss58_address=address),
    )
    assert loaded.platform_hotkey == PLATFORM.ss58_address
    assert loaded.platform_origin == "https://platform.example"


def _signed(value: HostedCodingResult | HostedCodingStatus) -> bytes:
    value = value.model_copy(
        update={"signature": PLATFORM.sign(hosted_signing_bytes(value)).hex()}
    )
    return (
        json.dumps(
            value.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def _receipt(request: HostedCodingRequest, *, terminal: bool) -> bytes:
    common = {
        "coding_contract_version": 2,
        "shadow_only": True,
        "weight_eligible": False,
        "evaluation_id": request.evaluation_id,
        "attempt_id": "20000000-0000-4000-8000-000000000002",
        "validator_hotkey": VALIDATOR.ss58_address,
        "platform_hotkey": PLATFORM.ss58_address,
        "request_sha256": hosted_message_digest(request),
        "artifact_sha256": "2" * 64,
        "assignment_sha256": request.assignment_sha256,
        "policy_sha256": "4" * 64,
        "execution_profile_sha256": "5" * 64,
        "grading_profile_sha256": "6" * 64,
        "issued_at_unix": NOW,
        "signature": "0" * 128,
    }
    if terminal:
        return _signed(
            HostedCodingResult.model_validate(
                {
                    **common,
                    "schema": "dittobench-coding-hosted-result-v2",
                    "evidence_sha256": "7" * 64,
                    "outcome": "completed",
                    "expires_at_unix": NOW + 3600,
                }
            )
        )
    return _signed(
        HostedCodingStatus.model_validate(
            {
                **common,
                "schema": "dittobench-coding-hosted-status-v2",
                "state": "started",
                "expires_at_unix": NOW + 120,
            }
        )
    )


def _respond(body: bytes, status: int) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Cache-Control": "no-store", "Content-Type": "application/json"},
        stream=Chunks([body]),
    )


def _args(tmp_path: Path, operation: str, **extra: Any) -> argparse.Namespace:
    return argparse.Namespace(
        operation=operation,
        assignment=write_assignment(tmp_path),
        assignment_sha256=control.assignment_sha256(projection()),
        result_out=extra.pop("result_out", None),
        **extra,
    )


async def test_evaluate_signs_once_with_the_in_place_key(tmp_path: Path) -> None:
    signer = Signer()
    sent: list[HostedCodingRequest] = []

    def respond(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        sent.append(request)
        assert VALIDATOR.verify(
            hosted_signing_bytes(request), bytes.fromhex(request.signature)
        )
        return _respond(_receipt(request, terminal=False), 202)

    summary = await control.run_command(
        _args(tmp_path, "evaluate"),
        context(signer),
        clock=lambda: NOW,
        transport=httpx.MockTransport(respond),
    )
    assert summary == {
        "operation": "evaluate",
        "terminal": False,
        "state": "started",
        "assignment_expired": False,
        "shadow_only": True,
        "weight_eligible": False,
    }
    assert [r.operation for r in sent] == ["evaluate"] and signer.signed == 1
    # Backdated for clock skew; the signed window stays within Platform's 120 s.
    assert sent[0].issued_at_unix == NOW - control.REQUEST_BACKDATE_SECONDS
    assert sent[0].expires_at_unix == NOW + control.REQUEST_LIFETIME_SECONDS
    assert 0 < sent[0].expires_at_unix - sent[0].issued_at_unix <= 120


async def test_evaluate_refuses_an_expired_assignment_before_signing(
    tmp_path: Path,
) -> None:
    signer = Signer()
    with pytest.raises(control.HostedControlCommandError):
        await control.run_command(
            _args(tmp_path, "evaluate"),
            context(signer),
            clock=lambda: NOW + 1800,
            transport=httpx.MockTransport(lambda _: pytest.fail("network used")),
        )
    assert signer.signed == 0


async def test_status_polls_boundedly_and_writes_an_owner_only_result(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out"
    output.mkdir(mode=0o700)
    responses = iter([False, True])
    sleeps: list[float] = []

    def respond(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        terminal = next(responses)
        return _respond(_receipt(request, terminal=terminal), 200 if terminal else 202)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    summary = await control.run_command(
        _args(tmp_path, "status", result_out=output / "result.json", wait_seconds=600),
        context(),
        clock=lambda: NOW,
        sleep=sleep,
        transport=httpx.MockTransport(respond),
    )
    assert summary["terminal"] is True and summary["outcome"] == "completed"
    assert sleeps == [control.POLL_INTERVAL_SECONDS]
    written = output / "result.json"
    assert oct(written.stat().st_mode & 0o777) == "0o600"
    result = HostedCodingResult.model_validate_json(written.read_bytes())
    assert hosted_message_digest(result) == summary["result_sha256"]


async def test_status_without_waiting_returns_pending_and_never_retries(
    tmp_path: Path,
) -> None:
    calls = 0

    def respond(outgoing: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        return _respond(_receipt(request, terminal=False), 202)

    summary = await control.run_command(
        _args(tmp_path, "status", result_out=tmp_path / "r.json", wait_seconds=0),
        context(),
        clock=lambda: NOW,
        transport=httpx.MockTransport(respond),
    )
    assert summary["terminal"] is False and calls == 1


async def test_status_refuses_an_existing_or_shared_result_path(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    existing = tmp_path / "private"
    existing.mkdir(mode=0o700)
    (existing / "result.json").write_text("{}")

    def respond(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        return _respond(_receipt(request, terminal=True), 200)

    for path in (
        shared / "result.json",
        existing / "result.json",
        Path("relative-result.json"),
    ):
        signer = Signer()
        with pytest.raises(control.HostedControlCommandError):
            await control.run_command(
                _args(tmp_path, "status", result_out=path, wait_seconds=3600),
                context(signer),
                clock=lambda: NOW,
                transport=httpx.MockTransport(lambda _: pytest.fail("network used")),
            )
        # Refused before any request is signed or sent.
        assert signer.signed == 0


async def test_acknowledge_binds_the_exact_verified_result(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir(mode=0o700)

    def terminal(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        return _respond(_receipt(request, terminal=True), 200)

    written = output / "result.json"
    await control.run_command(
        _args(tmp_path, "status", result_out=written, wait_seconds=0),
        context(),
        clock=lambda: NOW,
        transport=httpx.MockTransport(terminal),
    )
    result = HostedCodingResult.model_validate_json(written.read_bytes())
    acknowledged: list[HostedCodingRequest] = []

    def acknowledge(outgoing: httpx.Request) -> httpx.Response:
        acknowledged.append(HostedCodingRequest.model_validate_json(outgoing.content))
        return httpx.Response(
            204, headers={"Cache-Control": "no-store"}, stream=Chunks([])
        )

    summary = await control.run_command(
        _args(tmp_path, "acknowledge", result=written),
        context(),
        clock=lambda: NOW + 60,
        transport=httpx.MockTransport(acknowledge),
    )
    assert summary["acknowledged"] is True
    assert acknowledged[0].operation == "acknowledge"
    assert acknowledged[0].result_sha256 == hosted_message_digest(result)

    tampered = json.loads(written.read_text())
    tampered["outcome"] = "candidate_failure"
    forged = output / "forged.json"
    fd = os.open(forged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n")
    # A forged result is refused by verification before any request is sent.
    with pytest.raises((control.HostedControlCommandError, HostedCodingTransportError)):
        await control.run_command(
            _args(tmp_path, "acknowledge", result=forged),
            context(),
            clock=lambda: NOW + 60,
            transport=httpx.MockTransport(lambda _: pytest.fail("network used")),
        )


async def test_command_refuses_an_assignment_for_another_validator(
    tmp_path: Path,
) -> None:
    other = projection()
    other["validator_hotkey"] = PLATFORM.ss58_address
    path = write_assignment(tmp_path, other)
    args = argparse.Namespace(
        operation="evaluate",
        assignment=path,
        assignment_sha256=control.assignment_sha256(other),
        result_out=None,
    )
    signer = Signer()
    with pytest.raises(control.HostedControlCommandError):
        await control.run_command(
            args,
            context(signer),
            clock=lambda: NOW,
            transport=httpx.MockTransport(lambda _: pytest.fail("network used")),
        )
    assert signer.signed == 0


def test_main_is_disabled_by_default_and_prints_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv(control.ENABLED_ENV, raising=False)
    status = control.main(
        [
            "evaluate",
            "--validator-hotkey",
            VALIDATOR.ss58_address,
            "--assignment",
            str(write_assignment(tmp_path)),
            "--assignment-sha256",
            control.assignment_sha256(projection()),
        ]
    )
    captured = capsys.readouterr()
    assert status == control.EXIT_REFUSED
    assert captured.out == ""
    assert "disabled" in captured.err


def test_main_rejects_unbounded_waits_and_unknown_operations(
    capsys: pytest.CaptureFixture,
) -> None:
    for argv in (
        [
            "status",
            "--validator-hotkey",
            VALIDATOR.ss58_address,
            "--assignment",
            "/nonexistent",
            "--assignment-sha256",
            "0" * 64,
            "--result-out",
            "/nonexistent/r.json",
            "--wait-seconds",
            "3601",
        ],
        ["export-seed"],
    ):
        assert control.main(argv) == control.EXIT_REFUSED
    assert "arguments are invalid" in capsys.readouterr().err


def test_worker_never_imports_the_command() -> None:
    root = Path(__file__).parents[3]
    for path in (root / "ditto/validator").glob("*.py"):
        if path.name in {"coding_hosted_control.py"}:
            continue
        assert "coding_hosted_control" not in path.read_text(), path


async def test_evaluate_reports_an_already_finished_attempt_without_refusing(
    tmp_path: Path,
) -> None:
    def respond(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        return _respond(_receipt(request, terminal=True), 200)

    summary = await control.run_command(
        _args(tmp_path, "evaluate"),
        context(),
        clock=lambda: NOW,
        transport=httpx.MockTransport(respond),
    )
    assert summary["terminal"] is True and summary["result_saved"] is False
    assert summary["outcome"] == "completed"


async def test_receipts_are_accepted_within_bounded_clock_skew(tmp_path: Path) -> None:
    for platform_ahead, accepted in (
        (20, True),
        (control.HOSTED_CLOCK_SKEW_SECONDS + 5, False),
    ):

        def respond(
            outgoing: httpx.Request, ahead: int = platform_ahead
        ) -> httpx.Response:
            request = HostedCodingRequest.model_validate_json(outgoing.content)
            status = HostedCodingStatus.model_validate_json(
                _receipt(request, terminal=False)
            )
            shifted = status.model_copy(
                update={
                    "issued_at_unix": NOW + ahead,
                    "expires_at_unix": NOW + ahead + 120,
                }
            )
            return _respond(_signed(shifted), 202)

        call = control.run_command(
            _args(tmp_path, "evaluate"),
            context(),
            clock=lambda: NOW,
            transport=httpx.MockTransport(respond),
        )
        if accepted:
            assert (await call)["state"] == "started"
        else:
            with pytest.raises(HostedCodingTransportError):
                await call


async def test_status_stops_polling_an_unstarted_attempt_after_its_deadline(
    tmp_path: Path,
) -> None:
    calls = 0

    def respond(outgoing: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        status = HostedCodingStatus.model_validate_json(
            _receipt(request, terminal=False)
        ).model_copy(
            update={
                "state": "admitted",
                "issued_at_unix": NOW + 1800,
                "expires_at_unix": NOW + 1920,
            }
        )
        return _respond(_signed(status), 202)

    output = tmp_path / "out"
    output.mkdir(mode=0o700)
    summary = await control.run_command(
        _args(tmp_path, "status", result_out=output / "r.json", wait_seconds=3600),
        context(),
        clock=lambda: NOW + 1800,
        sleep=lambda _: pytest.fail("kept polling"),
        transport=httpx.MockTransport(respond),
    )
    assert calls == 1
    assert summary["terminal"] is False and summary["assignment_expired"] is True


async def test_failed_result_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir(mode=0o700)

    def respond(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        return _respond(_receipt(request, terminal=True), 200)

    def failing_fsync(_: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(control.os, "fsync", failing_fsync)
    with pytest.raises(control.HostedControlCommandError):
        await control.run_command(
            _args(
                tmp_path, "status", result_out=output / "result.json", wait_seconds=0
            ),
            context(),
            clock=lambda: NOW,
            transport=httpx.MockTransport(respond),
        )
    assert not (output / "result.json").exists()


async def test_acknowledge_refuses_an_expired_result_before_network(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out"
    output.mkdir(mode=0o700)

    def terminal(outgoing: httpx.Request) -> httpx.Response:
        request = HostedCodingRequest.model_validate_json(outgoing.content)
        return _respond(_receipt(request, terminal=True), 200)

    written = output / "result.json"
    await control.run_command(
        _args(tmp_path, "status", result_out=written, wait_seconds=0),
        context(),
        clock=lambda: NOW,
        transport=httpx.MockTransport(terminal),
    )
    signer = Signer()
    with pytest.raises(control.HostedControlCommandError) as caught:
        await control.run_command(
            _args(tmp_path, "acknowledge", result=written),
            context(signer),
            clock=lambda: NOW + 3600 + control.HOSTED_CLOCK_SKEW_SECONDS,
            transport=httpx.MockTransport(lambda _: pytest.fail("network used")),
        )
    assert "expired" in str(caught.value) and signer.signed == 0


async def test_fifo_inputs_are_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(control.HostedControlCommandError):
        control.load_assignment(fifo, control.assignment_sha256(projection()))
    with pytest.raises(control.HostedControlCommandError):
        await control.run_command(
            _args(tmp_path, "acknowledge", result=fifo),
            context(),
            clock=lambda: NOW,
            transport=httpx.MockTransport(lambda _: pytest.fail("network used")),
        )
