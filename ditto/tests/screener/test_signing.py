"""Tests for screener verdict signing (message format + delegation)."""

from __future__ import annotations

from uuid import UUID

from ditto.screener.signing import sign_verdict, verdict_signing_message

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")


def test_message_matches_platform_format() -> None:
    # Must byte-for-byte match the platform's
    # f"{screener_hotkey}:{agent_id}:{passed}".encode() — including Python's
    # bool str form ("True"/"False").
    msg = verdict_signing_message(screener_hotkey=_HOTKEY, agent_id=_AGENT, passed=True)
    assert msg == f"{_HOTKEY}:{_AGENT}:True".encode()
    assert msg.endswith(b":True")

    msg_false = verdict_signing_message(
        screener_hotkey=_HOTKEY, agent_id=_AGENT, passed=False
    )
    assert msg_false.endswith(b":False")


class _FakeKeypair:
    """Records what it was asked to sign; returns deterministic bytes."""

    def __init__(self) -> None:
        self.signed: bytes | None = None

    def sign(self, message: bytes) -> bytes:
        self.signed = message
        return b"\xab" * 64


def test_sign_verdict_signs_canonical_message() -> None:
    kp = _FakeKeypair()
    sig = sign_verdict(kp, screener_hotkey=_HOTKEY, agent_id=_AGENT, passed=False)
    assert sig == ("ab" * 64)
    assert kp.signed == f"{_HOTKEY}:{_AGENT}:False".encode()
