"""The canonical signed-score field order, and the legacy fallbacks around it."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from ditto.api_server.endpoints.validator import _score_signing_message


def _report(bench_version=None, transcript=None):
    details = {}
    if transcript is not None:
        details["transcript_sha256"] = transcript
    return SimpleNamespace(
        run_id="run-1",
        composite=0.5,
        seed=7,
        bench_version=bench_version,
        details=details,
    )


HOTKEY = "5" + "a" * 47
AGENT = uuid4()
SHA = "b" * 64


def _msg(**kw) -> str:
    return _score_signing_message(
        validator_hotkey=HOTKEY,
        agent_id=AGENT,
        report=_report(**kw),
        ticket_deadline=None,
    ).decode()


def test_canonical_order_is_bench_version_then_transcript() -> None:
    """Two independent changes each append a conditional suffix here, and both
    sides of the wire must agree byte-for-byte or every signature fails. The
    order is fixed deliberately: bench_version qualifies the seed, the
    transcript digest binds the artifact the run produced."""
    msg = _msg(bench_version=3, transcript=SHA)
    assert msg.endswith(f":7:3:{SHA}")


def test_legacy_report_signs_the_original_bytes() -> None:
    """A validator that sends neither field must produce exactly the
    pre-existing payload, or every old validator stops verifying."""
    assert _msg().endswith(":7")


def test_each_field_is_independently_optional() -> None:
    assert _msg(bench_version=3).endswith(":7:3")
    assert _msg(transcript=SHA).endswith(f":7:{SHA}")
