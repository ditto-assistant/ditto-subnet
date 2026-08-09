"""The canonical signed-score field order, and the legacy fallbacks around it."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

from ditto.api_models.validator import ScoreReport, V9BaseEvidence
from ditto.api_server.endpoints.scoring import _score_proof
from ditto.api_server.endpoints.validator import _score_signing_message
from ditto.db.models import Score


def _report(bench_version=None, transcript=None, base_evidence=None) -> ScoreReport:
    details = {}
    if transcript is not None:
        details["transcript_sha256"] = transcript
    return cast(
        ScoreReport,
        SimpleNamespace(
            run_id="run-1",
            composite=0.5,
            seed=7,
            bench_version=bench_version,
            base_evidence_sha256=base_evidence,
            details=details,
        ),
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


def test_v9_appends_base_evidence_after_transcript() -> None:
    base_evidence = "c" * 64
    assert _msg(bench_version=9, transcript=SHA, base_evidence=base_evidence).endswith(
        f":7:9:{SHA}:{base_evidence}"
    )


def test_v8_full_payload_remains_exact_when_v9_field_is_absent() -> None:
    assert _msg(bench_version=8, transcript=SHA).endswith(f":7:8:{SHA}")


def test_v8_report_reconstruction_and_ledger_json_combined_golden() -> None:
    """Freeze report details -> signed bytes -> public proof serialization."""
    deadline = datetime.fromisoformat("2026-08-08T12:34:56.123456+00:00")
    report = cast(
        ScoreReport,
        SimpleNamespace(
            run_id="run-v8-golden",
            composite=0.812345,
            seed=42,
            bench_version=8,
            base_evidence_sha256=None,
            details={"transcript_sha256": SHA},
        ),
    )
    assert (
        _score_signing_message(HOTKEY, AGENT, deadline, report)
        == (
            f"{HOTKEY}:{AGENT}:2026-08-08T12:34:56.123456+00:00:"
            f"run-v8-golden:0.812345:42:8:{SHA}"
        ).encode()
    )

    signature = "ab" * 64
    proof = _score_proof(
        cast(
            Score,
            SimpleNamespace(
                validator_hotkey=HOTKEY,
                run_id=report.run_id,
                composite=report.composite,
                seed=report.seed,
                bench_version=report.bench_version,
                signature=signature,
                details={
                    "ticket_deadline": deadline.isoformat(),
                    "transcript_sha256": SHA,
                },
            ),
        )
    )
    assert proof.model_dump_json() == (
        f'{{"validator_hotkey":"{HOTKEY}","run_id":"run-v8-golden",'
        '"composite":0.812345,"seed":42,"bench_version":8,'
        '"ticket_deadline":"2026-08-08T12:34:56.123456Z",'
        f'"transcript_sha256":"{SHA}","signature":"{signature}"}}'
    )


def test_platform_matches_shared_go_v9_base_and_signing_vector() -> None:
    vector_path = (
        Path(__file__).resolve().parents[6]
        / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
    )
    vector = json.loads(vector_path.read_text())["vectors"][0]
    evidence = V9BaseEvidence.model_validate(vector["details"])
    signing = vector["signing"]

    assert (
        evidence.score_gates.canonical_bytes().decode()
        == vector["score_gates_canonical"]
    )
    assert evidence.score_gates.digest_hex() == vector["score_gates_sha256"]
    assert evidence.canonical_bytes().decode() == vector["base_canonical"]
    assert evidence.digest_hex() == vector["base_evidence_sha256"]

    report = cast(
        ScoreReport,
        SimpleNamespace(
            run_id=signing["run_id"],
            composite=signing["composite"],
            seed=signing["seed"],
            bench_version=signing["bench_version"],
            base_evidence_sha256=signing["base_evidence_sha256"],
            details={"transcript_sha256": signing["transcript_sha256"]},
        ),
    )
    assert (
        _score_signing_message(
            signing["validator_hotkey"],
            UUID(signing["agent_id"]),
            datetime.fromisoformat(signing["ticket_deadline"]),
            report,
        ).decode()
        == signing["message"]
    )
