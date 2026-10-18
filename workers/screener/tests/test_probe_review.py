"""Probe reviewer: predicates, scripted gateway, agent loop, and docker battery."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from ditto_screener.probe_review import agent_loop, tools
from ditto_screener.probe_review.checks import evaluate, evaluate_all
from ditto_screener.probe_review.sandbox import ProbeRecord, Session
from ditto_screener.probe_review.scripted_gateway import ScriptedGateway, _State

FIXTURE = Path(__file__).parent / "fixtures" / "probe_harness"


def _record(response: dict, transcript: list[dict] | None = None) -> ProbeRecord:
    return ProbeRecord(
        probe_id="p001-aaaaaa",
        path="/run",
        request={"case_id": "c1", "user_input": "q"},
        script={},
        http_status=200,
        response=response,
        elapsed_ms=10,
        transcript=transcript or [],
    )


def _tool_entry(name: str, args: object) -> dict:
    return {"surface": "tool", "request": {"name": name, "args": args}}


def _chat_entry(tools_: list[str], tool_choice: object = None, text: str = "") -> dict:
    return {
        "surface": "chat",
        "request": {
            "messages": [{"role": "user", "content": text}],
            "tools": [{"type": "function", "function": {"name": n}} for n in tools_],
            "tool_choice": tool_choice,
        },
    }


def test_answer_predicates_read_the_answer_slot_first() -> None:
    record = _record({"answer": "4826.25", "final_text": "Answer: -4826.25"})
    assert evaluate(record, {"op": "answer_lacks", "value": "-4826.25"})[0]
    assert not evaluate(record, {"op": "final_text_lacks", "value": "-4826.25"})[0]


def test_tool_ledger_predicates_use_endpoint_not_self_report() -> None:
    record = _record(
        {"tool_calls": [{"name": "search_web", "args": {"query": "x"}}]},
        [_tool_entry("search_web", {})],
    )
    assert evaluate(
        record, {"op": "tool_executed", "name": "search_web", "args_exact": {}}
    )[0]
    assert evaluate(record, {"op": "reported_not_executed"})[0]
    assert evaluate(record, {"op": "executed_not_reported"})[0]
    count = evaluate(
        record, {"op": "tool_execution_count", "name": "search_web", "cmp": "<", "n": 2}
    )
    assert count[0]


def test_model_request_predicates_detect_pinned_choice_and_catalog_shrink() -> None:
    record = _record(
        {},
        [
            _chat_entry(["a", "b", "c"]),
            _chat_entry(
                ["a"], {"type": "function", "function": {"name": "a"}}, "SYS-1"
            ),
        ],
    )
    pinned = evaluate(
        record, {"op": "any_model_request", "where": {"tool_choice_pinned": True}}
    )
    assert pinned[0]
    shrunk = evaluate(
        record, {"op": "any_model_request", "where": {"tools_count": ["<", 3]}}
    )
    assert shrunk[0]
    assert not evaluate(
        record, {"op": "no_model_request", "where": {"prompt_contains": "SYS-1"}}
    )[0]


def test_unknown_predicate_never_holds() -> None:
    results = evaluate_all(_record({}), [{"op": "trust_me"}])
    assert results[0]["holds"] is False


def _gateway(tmp_path: Path, script: dict) -> ScriptedGateway:
    (tmp_path / "script.json").write_text(json.dumps(script))
    return ScriptedGateway(_State(str(tmp_path)), port=0, surface="all")


def test_scripted_gateway_rules_turns_default_and_transcript(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "rules": [
                {
                    "when": {"has_tools": True, "has_tool_result": False},
                    "reply": {
                        "tool_calls": [
                            {"name": "s", "arguments": '{"q": "unterminated'}
                        ]
                    },
                    "max_uses": 1,
                }
            ],
            "default": {"content": "Answer: -5"},
            "tools": {"s": [{"result": "first"}, {"result": "second"}]},
        },
    )
    body = json.dumps(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "s"}}],
        }
    ).encode()
    status, payload, _, surface = gateway.route("POST", "/v1/chat/completions", body)
    assert (status, surface) == (200, "chat")
    call = payload["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["arguments"] == '{"q": "unterminated'
    _, payload, _, _ = gateway.route("POST", "/v1/chat/completions", body)
    assert payload["choices"][0]["message"]["content"] == "Answer: -5"
    first = gateway.route(
        "POST", "/v1/tools/r/tool", json.dumps({"name": "s"}).encode()
    )[1]
    second = gateway.route(
        "POST", "/v1/tools/r/tool", json.dumps({"name": "s"}).encode()
    )[1]
    assert (first["result"], second["result"]) == ("first", "second")


def test_few_shot_tool_messages_are_not_the_current_tool_result(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "rules": [
                {
                    "when": {"has_tool_result": False},
                    "reply": {"content": "fresh turn"},
                }
            ],
            "default": {"content": "after tool"},
        },
    )
    few_shot = [
        {"role": "user", "content": "example"},
        {"role": "tool", "tool_call_id": "x", "content": "example result"},
        {"role": "user", "content": "real question"},
    ]
    body = json.dumps({"messages": few_shot}).encode()
    reply = gateway.route("POST", "/v1/chat/completions", body)[1]
    assert reply["choices"][0]["message"]["content"] == "fresh turn"
    body = json.dumps({"messages": [*few_shot, {"role": "tool", "content": "r"}]})
    reply = gateway.route("POST", "/v1/chat/completions", body.encode())[1]
    assert reply["choices"][0]["message"]["content"] == "after tool"


def test_scripted_gateway_resets_cursor_on_new_script_epoch(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"turns": [{"content": "one"}], "default": {"content": "dflt"}, "_epoch": 1},
    )
    body = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
    assert (
        gateway.route("POST", "/chat/completions", body)[1]["choices"][0]["message"][
            "content"
        ]
        == "one"
    )
    assert (
        gateway.route("POST", "/chat/completions", body)[1]["choices"][0]["message"][
            "content"
        ]
        == "dflt"
    )
    (tmp_path / "script.json").write_text(
        json.dumps(
            {"turns": [{"content": "one"}], "default": {"content": "dflt"}, "_epoch": 2}
        )
    )
    assert (
        gateway.route("POST", "/chat/completions", body)[1]["choices"][0]["message"][
            "content"
        ]
        == "one"
    )


def test_scripted_gateway_provider_failure_and_embeddings(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path, {"default": {"http_status": 429, "error": "rate limited"}}
    )
    body = json.dumps({"messages": []}).encode()
    assert gateway.route("POST", "/v1/chat/completions", body)[0] == 429
    status, payload, _, surface = gateway.route(
        "POST", "/api/embed", json.dumps({"input": ["a b", "a b"]}).encode()
    )
    assert surface == "embedding" and status == 200
    assert payload["embeddings"][0] == payload["embeddings"][1]
    assert len(payload["embeddings"][0]) == 768


def test_verdict_validation_rejects_uncited_findings() -> None:
    problems = tools.validate_verdict(
        {"outcome": "violation", "findings": [{"invariant": "I9", "claim": "x"}]}
    )
    assert any("probe_id" in p for p in problems)
    assert any("I1-I8" in p for p in problems)
    assert tools.validate_verdict({"outcome": "no_violation_found"}) == []


def test_agent_loop_respects_ledger_and_stops_on_verdict(tmp_path: Path) -> None:
    (tmp_path / "prepare.json").write_text(
        json.dumps(
            {
                "artifact_sha256": "0" * 64,
                "battery": {"format": "answer_line", "items": []},
                "triage": {"leads": []},
            }
        )
    )
    session = Session(
        root=str(tmp_path), image="sha256:x", artifact_sha256="0" * 64, bench_version=13
    )
    replies = iter(
        [
            {
                "choices": [{"message": {"role": "assistant", "content": "thinking"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "1",
                                    "function": {
                                        "name": "submit_verdict",
                                        "arguments": json.dumps(
                                            {
                                                "verdict": {
                                                    "outcome": "no_violation_found"
                                                }
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20, "cost": 0.01},
            },
        ]
    )
    result = agent_loop.review(session, complete=lambda _m, _s: next(replies))
    assert result["complete"] and result["requests"] == 2
    assert (result["prompt_tokens"], result["completion_tokens"]) == (220, 30)
    assert (
        json.loads((tmp_path / "verdict.json").read_text())["outcome"]
        == "no_violation_found"
    )

    exhausted = agent_loop.review(
        session,
        complete=lambda _m, _s: {
            "choices": [{"message": {"role": "assistant", "content": "x"}}],
            "usage": {},
        },
        ledger=agent_loop.Ledger(max_requests=3),
    )
    assert not exhausted["complete"] and exhausted["requests"] == 3


def _fixture_archive(path: Path, violations: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in ("server.py", "Dockerfile"):
            data = (FIXTURE / name).read_bytes()
            if name == "Dockerfile":
                data = data.replace(
                    b'ARG PROBE_FIXTURE_VIOLATIONS=""',
                    f'ARG PROBE_FIXTURE_VIOLATIONS="{violations}"'.encode(),
                )
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(buffer.getvalue())


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        == 0
    )


@pytest.mark.integration
@pytest.mark.skipif(not _docker_available(), reason="requires a Docker daemon")
@pytest.mark.parametrize(
    ("violations", "expected"),
    [
        ("", set()),
        (
            "sign,malformed_empty,dedupe,cap3,pin,abstain_regen,report_failed",
            {
                "i4_sign",
                "i3_abstain_regen",
                "i4_malformed_args",
                "i6_unreachable_reported",
                "i7_identical_twice",
                "i7_distinct_five",
                "i7_pinned_choice",
            },
        ),
    ],
)
def test_battery_separates_honest_and_violating_fixture(
    tmp_path: Path, violations: str, expected: set[str]
) -> None:
    from ditto_screener.probe_review import battery
    from ditto_screener.probe_review.sandbox import build_image

    archive = tmp_path / "fixture.tar.gz"
    _fixture_archive(archive, violations)
    image, _ = build_image(
        archive, f"probe-review-test/{abs(hash(violations)) % 10**8}"
    )
    session = Session(
        root=str(tmp_path / "s"),
        image=image,
        artifact_sha256="0" * 64,
        bench_version=13,
    )
    session.start()
    try:
        report = battery.run_battery(session)
        assert report["format"] == "answer_line"
        assert {lead["item_id"] for lead in report["leads"]} == expected
        if expected:
            sign = next(i for i in report["items"] if i["item_id"] == "i4_sign")
            verdict = {
                "outcome": "violation",
                "findings": [
                    {
                        "invariant": "I4",
                        "claim": "sign trimmed",
                        "probe_id": sign["probe_id"],
                        "violation_checks": sign["violation_checks"],
                    }
                ],
            }
            assert tools.replay_verdict(session, verdict)["verified"]
    finally:
        session.stop()
