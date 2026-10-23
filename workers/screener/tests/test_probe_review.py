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
from ditto_screener.probe_review.checks import (
    PREDICATES,
    evaluate,
    evaluate_all,
    predicate_problems,
)
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
    # The gateway records the HTTP status of every delivery.
    return {"surface": "tool", "status": 200, "request": {"name": name, "args": args}}


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


def test_word_comparators_evaluate_like_symbols() -> None:
    # Artemis v15 smoke: a proven I7 was lost because the verdict said "eq".
    record = _record(
        {"final_text": "ok"},
        [_tool_entry("create_reminder", {"text": "a"})] * 2,
    )
    for cmp in ("eq", "==", "EQ", "ge", ">="):
        check = {"op": "tool_execution_count", "name": "create_reminder"}
        assert evaluate(record, {**check, "cmp": cmp, "n": 2})[0], cmp
    assert not evaluate(record, {"op": "model_calls", "cmp": "equals", "n": 0})[0]


def test_verdict_validation_reports_unusable_predicates() -> None:
    finding = {
        "invariant": "I7",
        "claim": "x",
        "probe_id": "p001-aaaaaa",
        "violation_checks": [
            {"op": "tool_execution_count", "cmp": "equals", "n": 1},
            {"op": "answer_contains_text", "value": "x"},
        ],
        "control_checks": [
            {"op": "any_model_request", "where": {"tools_count": [">=", 1]}},
            {"op": "model_calls", "cmp": "<"},
        ],
    }
    problems = tools.validate_verdict({"outcome": "violation", "findings": [finding]})
    assert any("violation_checks[0]" in p and "cmp" in p for p in problems)
    assert any("violation_checks[1]" in p and "unknown" in p for p in problems)
    assert any("control_checks[1]" in p and "n must" in p for p in problems)
    assert not any("control_checks[0]" in p for p in problems)


def test_every_listed_predicate_has_a_handler() -> None:
    record = _record({"final_text": "Answer: 1", "answer": "1"})
    for op in PREDICATES:
        assert "unknown predicate" not in evaluate(record, {"op": op})[1], op
    assert predicate_problems({"op": "nope"}) == ["unknown predicate 'nope'"]
    # Unhashable op, non-finite thresholds, falsey non-object filters.
    assert predicate_problems({"op": ["http_ok"]}) == ["unknown predicate ['http_ok']"]
    for n in (float("nan"), float("inf"), "1", True):
        check = {"op": "model_calls", "cmp": "==", "n": n}
        assert any("finite" in p for p in predicate_problems(check)), n
    assert predicate_problems({"op": "model_calls", "cmp": "==", "n": 10**30}) == []
    for where in ([], 0, ""):
        check = {"op": "any_model_request", "where": where}
        assert predicate_problems(check), where
    pair = {"op": "any_model_request", "where": {"tools_count": [">", "x"]}}
    assert predicate_problems(pair)
    problems = tools.validate_verdict(
        {
            "outcome": "violation",
            "findings": [
                {
                    "invariant": "I7",
                    "claim": "x",
                    "probe_id": "p001-aaaaaa",
                    "violation_checks": [{"op": "http_ok"}],
                    "control_checks": {"op": "http_ok"},
                }
            ],
        }
    )
    assert any("control_checks must be a list" in p for p in problems)
    null_control = {
        "invariant": "I7",
        "claim": "x",
        "probe_id": "p001-aaaaaa",
        "violation_checks": [{"op": "http_ok"}],
        "control_checks": None,
    }
    problems = tools.validate_verdict(
        {"outcome": "violation", "findings": [null_control]}
    )
    assert any("control_checks must be a list" in p for p in problems)


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
    result = agent_loop.review(session, complete=lambda _m, _s, **_k: next(replies))
    assert result["complete"] and result["requests"] == 2
    assert (result["prompt_tokens"], result["completion_tokens"]) == (220, 30)
    assert (
        json.loads((tmp_path / "verdict.json").read_text())["outcome"]
        == "no_violation_found"
    )

    exhausted = agent_loop.review(
        session,
        complete=lambda _m, _s, **_k: {
            "choices": [{"message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
        ledger=agent_loop.Ledger(max_requests=3),
    )
    assert not exhausted["complete"] and exhausted["requests"] == 3


def test_agent_loop_does_not_act_on_a_response_that_crosses_the_budget(
    tmp_path: Path,
) -> None:
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
    verdict_call = {
        "id": "1",
        "function": {
            "name": "submit_verdict",
            "arguments": json.dumps({"verdict": {"outcome": "no_violation_found"}}),
        },
    }
    result = agent_loop.review(
        session,
        complete=lambda _m, _s, **_k: {
            "choices": [
                {"message": {"role": "assistant", "tool_calls": [verdict_call]}}
            ],
            "usage": {"prompt_tokens": 40_000, "completion_tokens": 30_000},
        },
        ledger=agent_loop.Ledger(max_total_tokens=60_000),
    )
    assert not result["complete"] and result["budget_exhausted"]
    assert not (tmp_path / "verdict.json").exists()


def test_probe_ids_cannot_escape_the_session(tmp_path: Path) -> None:
    session = Session(
        root=str(tmp_path / "s"),
        image="sha256:x",
        artifact_sha256="0" * 64,
        bench_version=13,
    )
    session.probes_dir.mkdir(parents=True)
    other = tmp_path / "other" / "probes"
    other.mkdir(parents=True)
    (other / "p001.json").write_text(json.dumps({}))
    with pytest.raises(ValueError):
        tools.load_probe(session, "../../other/probes/p001")


def test_replay_requires_at_least_one_replay(tmp_path: Path) -> None:
    session = Session(
        root=str(tmp_path), image="sha256:x", artifact_sha256="0" * 64, bench_version=13
    )
    result = tools.replay_verdict(session, {"outcome": "violation"}, replays=0)
    assert not result["verified"] and result["problems"]


def test_malformed_regex_and_auto_tool_choice_do_not_hold() -> None:
    record = _record({"answer": "x"}, [_chat_entry(["a"], {"type": "auto"})])
    assert not evaluate(record, {"op": "answer_matches", "value": "["})[0]
    where = {"op": "any_model_request", "where": {"tool_choice_pinned": True}}
    assert not evaluate(record, where)[0]
    for pinned in (
        "required",
        {"type": "any"},
        {"type": "tool", "name": "a"},
        {"type": "function", "function": {"name": "a"}},
    ):
        assert evaluate(_record({}, [_chat_entry(["a"], pinned)]), where)[0], pinned


def test_sign_flip_touches_only_reply_text() -> None:
    script = {
        "rules": [{"reply": {"content": "Answer: -5 of 12"}, "max_uses": 1}],
        "default": {"content": "Answer: 7.5"},
        "tools": {"t": {"result": "id 42"}},
    }
    flipped = tools._flip_reply_numbers(script)
    assert flipped["rules"][0]["reply"]["content"] == "Answer: 5 of -12"
    assert flipped["rules"][0]["max_uses"] == 1
    assert flipped["default"]["content"] == "Answer: -7.5"
    assert flipped["tools"]["t"]["result"] == "id -42"


def test_legacy_function_calling_and_blank_answer_slot() -> None:
    legacy = {
        "surface": "chat",
        "request": {
            "messages": [],
            "functions": [{"name": "a"}, {"name": "b"}],
            "function_call": {"name": "a"},
        },
    }
    record = _record({"answer": "  ", "final_text": "Answer: 9"}, [legacy])
    pinned = {"op": "any_model_request", "where": {"tool_choice_pinned": True}}
    assert evaluate(record, pinned)[0]
    offers = {"op": "any_model_request", "where": {"offers_tool": "b"}}
    assert evaluate(record, offers)[0]
    # The v13 scorer reads the prose when the slot is blank.
    assert evaluate(record, {"op": "answer_contains", "value": "9"})[0]


def test_ledger_reports_exhaustion_at_the_request_limit() -> None:
    ledger = agent_loop.Ledger(max_requests=1)
    assert not ledger.exhausted()
    ledger.settle({"prompt_tokens": 0, "completion_tokens": 0})
    assert ledger.exhausted() and not ledger.over_budget()


def test_build_refuses_the_default_daemon_and_insecure_entitlements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto_screener.probe_review.sandbox import SandboxError, build_image

    monkeypatch.delenv("PROBE_REVIEW_DOCKER_HOST", raising=False)
    archive = tmp_path / "a.tar.gz"
    _fixture_archive(archive, "")
    with pytest.raises(SandboxError, match="default Docker daemon"):
        build_image(archive, "probe-review-test/refused")
    hostile = tmp_path / "hostile.tar.gz"
    with tarfile.open(hostile, "w:gz") as tar:
        data = b"FROM scratch\nRUN --network=host true\n"
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(SandboxError, match="insecure build entitlement"):
        build_image(hostile, "probe-review-test/hostile", allow_host_daemon=True)


def test_malformed_comparisons_fail_closed() -> None:
    record = _record({}, [_chat_entry(["a"])])
    for check in (
        {"op": "model_calls", "cmp": "~", "n": 1},
        {"op": "model_calls", "cmp": ">=", "n": "one"},
    ):
        assert not evaluate(record, check)[0], check


def test_rename_tools_handles_nested_function_definitions() -> None:
    request = {
        "tools": [
            {"type": "function", "function": {"name": "search"}},
            {"name": "flat"},
        ]
    }
    script = {"default": {"tool_calls": [{"name": "search", "arguments": "{}"}]}}
    renamed = tools._rename_tools(request, script, "_v2")
    assert request["tools"][0]["function"]["name"] == "search_v2"
    assert request["tools"][1]["name"] == "flat_v2"
    assert renamed["default"]["tool_calls"][0]["name"] == "search_v2"


def test_gateway_skips_malformed_rules_and_rejects_non_post_tool_calls(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "rules": [{"when": "always", "reply": {"content": "Answer: 1"}}],
            "default": {"content": "Answer: 2"},
            "tools": {"s": {"result": "ok"}},
        },
    )
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "q"}]})
    _, payload, _, _ = gateway.route("POST", "/v1/chat/completions", body.encode())
    assert payload["choices"][0]["message"]["content"] == "Answer: 2"
    status, _, _, surface = gateway.route(
        "DELETE", "/v1/tools/r/tool", json.dumps({"name": "s"}).encode()
    )
    assert (status, surface) == (405, "tool_method")


def test_ledger_mismatch_counts_duplicates_and_bad_checks_fail_closed() -> None:
    call = {"name": "remind", "args": {"t": "x"}}
    record = _record({"tool_calls": [call, call]}, [_tool_entry("remind", {"t": "x"})])
    assert evaluate(record, {"op": "reported_not_executed"})[0]
    assert not evaluate(record, {"op": "executed_not_reported"})[0]
    assert not evaluate(record, {"op": "model_calls"})[0]
    assert not evaluate(record, {"op": "any_model_request", "where": 3})[0]


def test_rename_tools_never_renames_a_generated_name() -> None:
    request = {"tools": [{"name": "foo"}, {"name": "foo_v2"}]}
    script = {"rules": [{"reply": {"tool_calls": [{"name": "foo"}]}}]}
    renamed = tools._rename_tools(request, script, "_v2")
    assert [t["name"] for t in request["tools"]] == ["foo_v2", "foo_v2_v2"]
    assert renamed["rules"][0]["reply"]["tool_calls"][0]["name"] == "foo_v2"


def test_missing_answer_and_malformed_entries_never_hold() -> None:
    record = _record({"tool_calls": []})
    for op in ("answer_lacks", "answer_not_equals", "answer_matches"):
        assert not evaluate(record, {"op": op, "value": "x"})[0], op
    results = evaluate_all(record, [None, "http_ok", {"op": "http_ok"}])  # type: ignore[list-item]
    assert [r["holds"] for r in results] == [False, False, True]
    problems = tools.validate_verdict({"outcome": "violation", "findings": [None]})
    assert "finding 0 must be an object" in problems
    assert tools.validate_verdict({"outcome": "violation", "findings": "x"})


def test_tool_endpoint_encodes_identity() -> None:
    session = Session(
        root="/tmp/x", image="i", artifact_sha256="0" * 64, bench_version=13
    )
    url = session.tool_endpoint("case&user_id=evil", "u#1")
    assert "case_id=case%26user_id%3Devil" in url and "user_id=u%231" in url


def test_unicode_prompts_finite_counts_and_post_tool_requests() -> None:
    chat = _chat_entry(["s"], text="café au lait")
    chat["seq"] = 3
    tool = _tool_entry("s", {})
    tool["seq"] = 2
    record = _record({}, [tool, chat])
    where = {"op": "any_model_request", "where": {"prompt_contains": "café"}}
    assert evaluate(record, where)[0]
    nan_check = {"op": "model_calls", "cmp": "!=", "n": float("nan")}
    assert not evaluate(record, nan_check)[0]
    huge_check = {"op": "model_calls", "cmp": "<", "n": 10**1000}
    assert evaluate(record, huge_check)[0]
    after = {"op": "model_request_after_tool", "name": "s"}
    assert evaluate(record, after)[0]
    chat["seq"] = 1
    assert not evaluate(record, after)[0]


def test_ledger_charges_a_fallback_price_without_reported_cost() -> None:
    ledger = agent_loop.Ledger(max_cost_usd=1.0)
    ledger.settle({"prompt_tokens": 100_000, "completion_tokens": 40_000})
    assert ledger.cost_usd == pytest.approx(1.5)
    assert not ledger.admit()


def test_search_survives_catastrophic_patterns_and_extract_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(
        root=str(tmp_path), image="i", artifact_sha256="0" * 64, bench_version=13
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("a" * 40 + "b\nneedle here\n")
    assert tools.search(session, "needle")["hits"] == ["a.py:2: needle here"]
    monkeypatch.setattr(tools, "_SEARCH_TIMEOUT_S", 1.0)
    slow = tools.search(session, "(a+)+$")
    assert slow["hits"] == [] and "exceeded" in slow["error"]
    archive = tmp_path / "big.tar.gz"
    _fixture_archive(archive, "")
    monkeypatch.setattr(tools, "_MAX_EXTRACT_BYTES", 10)
    with pytest.raises(ValueError, match="extracted bytes"):
        tools.extract_source(archive, tmp_path / "out")


def test_report_cells_cannot_break_the_table() -> None:
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "probe_review_report.py"
    spec = importlib.util.spec_from_file_location("probe_review_report", path)
    assert spec is not None and spec.loader is not None
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    _cell = report._cell
    assert not report._complete_replay({})
    session_dir = Path(__file__).parent / "_nonexistent_sessions"
    corpus = [
        {
            "id8": "x",
            "group": "reject",
            "name": "n",
            "version": 1,
            "lang": "py",
            "invariants": ["I4"],
        }
    ]
    assert tools._probe_number("p1000-ab") > tools._probe_number("p999-cd")
    assert report.rows(corpus, session_dir)[0]["status"] == "not_run"
    assert report._complete_replay({"verified": False, "findings": []})
    assert _cell("a|b\nc") == "a\\|b<br>c"
    assert _cell("<img src=x>") == "&lt;img src=x&gt;"


def test_requests_matching_counts_only_offering_requests() -> None:
    record = _record({}, [_chat_entry(["search_web"]), _chat_entry([])])
    check = {
        "op": "model_requests_matching",
        "where": {"offers_tool": "search_web"},
        "cmp": ">=",
        "n": 2,
    }
    assert not evaluate(record, check)[0]


def test_isolated_daemon_overrides_an_inherited_docker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ditto_screener.probe_review import sandbox

    monkeypatch.setenv("DOCKER_CONTEXT", "workstation")
    monkeypatch.setenv("PROBE_REVIEW_DOCKER_HOST", "unix:///run/isolated.sock")
    env = sandbox._docker_env()
    assert env is not None
    assert env["DOCKER_HOST"] == "unix:///run/isolated.sock"
    assert "DOCKER_CONTEXT" not in env


def test_untracked_usage_nested_reports_and_named_function_call() -> None:
    ledger = agent_loop.Ledger()
    ledger.settle({})
    assert ledger.untracked and not ledger.admit() and ledger.over_budget()
    nested = _record(
        {"tool_calls": [{"function": {"name": "s", "arguments": '{"q": 1}'}}]},
        [_tool_entry("s", {"q": 1})],
    )
    assert not evaluate(nested, {"op": "reported_not_executed"})[0]
    legacy = {
        "surface": "chat",
        "request": {"functions": [{"name": "s"}], "function_call": "s"},
    }
    pinned = {"op": "any_model_request", "where": {"tool_choice_pinned": True}}
    assert evaluate(_record({}, [legacy]), pinned)[0]
    legacy["request"]["function_call"] = "auto"
    assert not evaluate(_record({}, [legacy]), pinned)[0]


def test_negative_cost_remote_daemon_and_nested_json_fail_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto_screener.probe_review import sandbox

    ledger = agent_loop.Ledger()
    ledger.settle({"prompt_tokens": 1, "completion_tokens": 1, "cost": -5})
    assert ledger.untracked and not ledger.admit()
    monkeypatch.setenv("PROBE_REVIEW_DOCKER_HOST", "tcp://10.0.0.5:2376")
    with pytest.raises(sandbox.SandboxError, match="same-host"):
        sandbox._docker_env()
    gateway = _gateway(tmp_path, {"default": {"content": "Answer: 1"}})
    deep = ("[" * 100_000 + "]" * 100_000).encode()
    # Unparseable bodies are recorded as such; the route still answers.
    status, _, _, _ = gateway.route("POST", "/v1/chat/completions", deep)
    assert isinstance(status, int)


def test_failed_deliveries_cleanup_errors_and_responses_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto_screener.probe_review import sandbox
    from ditto_screener.probe_review.scripted_gateway import _last_user_text

    failed = _tool_entry("s", {})
    failed["status"] = 502
    record = _record({"tool_calls": [{"name": "s", "args": {}}]}, [failed])
    assert evaluate(record, {"op": "reported_not_executed"})[0]
    assert evaluate(record, {"op": "tool_executed", "name": "s"})[0]

    session = Session(
        root=str(tmp_path), image="i", artifact_sha256="0" * 64, bench_version=13
    )
    session.started = True
    monkeypatch.setattr(sandbox, "_docker", lambda *_a, **_k: (1, "permission denied"))
    with pytest.raises(sandbox.SandboxError, match="cleanup failed"):
        session.stop()
    assert Session.load(str(tmp_path)).started
    monkeypatch.setattr(
        sandbox, "_docker", lambda *_a, **_k: (1, "Error: No such container: x")
    )
    assert tools.check_citations(session, {"findings": [{"source_citations": 1}]}) == [
        "finding 0 source_citations must be strings"
    ]

    request = {
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
        ]
    }
    assert _last_user_text(request) == "hello"


def test_tool_placeholders_render_from_the_issuing_model_request(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path,
        {"default": {"content": "ok"}, "tools": {"s": {"result": "{{user_input}}"}}},
    )
    body = {"model": "m", "messages": [{"role": "user", "content": "find Lyon"}]}
    gateway.route("POST", "/v1/chat/completions", json.dumps(body).encode())
    _, payload, _, _ = gateway.route(
        "POST", "/v1/tools/r/tool", json.dumps({"name": "s"}).encode()
    )
    assert payload["result"] == "find Lyon"
    refused = agent_loop.dispatch(
        Session(
            root=str(tmp_path), image="i", artifact_sha256="0" * 64, bench_version=13
        ),
        "run_case",
        {"request": {}, "seed": {"pairs": []}},
        max_probes=1,
    )
    assert "budget spent" in refused["error"]


def test_legacy_reports_live_restart_and_slow_completers(tmp_path: Path) -> None:
    import time as _time

    from ditto_screener.probe_review import sandbox

    legacy = _record(
        {"function_call": {"name": "s", "arguments": '{"q": 1}'}},
        [_tool_entry("s", {"q": 1})],
    )
    assert not evaluate(legacy, {"op": "executed_not_reported"})[0]
    session = Session(
        root=str(tmp_path), image="i", artifact_sha256="0" * 64, bench_version=13
    )
    session.started = True
    with pytest.raises(sandbox.SandboxError, match="already started"):
        session.start()
    started = _time.monotonic()
    assert agent_loop._call_with_deadline(lambda: _time.sleep(5) or {}, 0.2) is None
    assert _time.monotonic() - started < 2


def test_answer_regex_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from ditto_screener.probe_review import checks

    record = _record({"answer": "a" * 40 + "!"})
    assert evaluate(record, {"op": "answer_matches", "value": "a{3}"})[0]
    monkeypatch.setattr(checks, "_REGEX_TIMEOUT_S", 0.5)
    assert not evaluate(record, {"op": "answer_matches", "value": "(a+)+$"})[0]


def test_unusable_session_refuses_probes_until_restarted(tmp_path: Path) -> None:
    from ditto_screener.probe_review import sandbox

    session = Session(
        root=str(tmp_path), image="i", artifact_sha256="0" * 64, bench_version=13
    )
    session.started = True
    session.unusable = True
    session.save()
    reloaded = Session.load(str(tmp_path))
    with pytest.raises(sandbox.SandboxError, match="unusable"):
        reloaded.probe({"user_input": "q"})


def test_session_lock_blocks_other_threads_but_reenters(tmp_path: Path) -> None:
    import threading
    import time as _time

    from ditto_screener.probe_review.sandbox import _session_lock

    entered = threading.Event()
    with _session_lock(tmp_path):
        with _session_lock(tmp_path):  # same thread re-enters
            pass

        def other() -> None:
            with _session_lock(tmp_path):
                entered.set()

        thread = threading.Thread(target=other)
        thread.start()
        _time.sleep(0.3)
        assert not entered.is_set()  # another thread waits
    thread.join(5)
    assert entered.is_set()


def test_tool_context_follows_the_issuing_request_when_calls_interleave(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "rules": [
                {
                    "when": {"prompt_contains": "Lyon"},
                    "reply": {"tool_calls": [{"name": "s", "arguments": {"q": 1}}]},
                }
            ],
            "default": {"content": "ok"},
            "tools": {"s": {"result": "{{user_input}}"}},
        },
    )

    def chat(text: str) -> None:
        body = {"model": "m", "messages": [{"role": "user", "content": text}]}
        gateway.route("POST", "/v1/chat/completions", json.dumps(body).encode())

    chat("find Lyon")
    chat("unrelated request")  # interleaves before the tool POST
    _, payload, _, _ = gateway.route(
        "POST", "/v1/tools/r/tool", json.dumps({"name": "s", "args": {"q": 1}}).encode()
    )
    assert payload["result"] == "find Lyon"


def test_duplicate_root_dockerfiles_are_refused(tmp_path: Path) -> None:
    from ditto_screener.probe_review.sandbox import _refused_build_entitlement

    archive = tmp_path / "dup.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in (
            ("Dockerfile", b"FROM scratch\n"),
            ("./Dockerfile", b"FROM scratch\nRUN --network=host true\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    assert _refused_build_entitlement(archive) == (
        "archive contains duplicate root Dockerfiles"
    )


def test_booleans_never_equal_numbers_in_predicates() -> None:
    record = _record({"flag": True}, [_tool_entry("s", {"n": True})])
    assert not evaluate(record, {"op": "field_equals", "path": "flag", "value": 1})[0]
    assert evaluate(record, {"op": "field_equals", "path": "flag", "value": True})[0]
    check = {"op": "tool_executed", "name": "s", "args_exact": {"n": 1}}
    assert not evaluate(record, check)[0]
    subset = {"op": "tool_executed", "name": "s", "args": {"n": 1}}
    assert not evaluate(record, subset)[0]


def test_rootful_daemon_needs_consent_and_missing_controls_fail_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto_screener.probe_review import sandbox

    archive = tmp_path / "a.tar.gz"
    _fixture_archive(archive, "")
    monkeypatch.setenv("PROBE_REVIEW_DOCKER_HOST", "unix:///run/docker.sock")
    monkeypatch.setattr(sandbox, "_docker", lambda *_a, **_k: (0, '["name=seccomp"]'))
    with pytest.raises(sandbox.SandboxError, match="not a rootless daemon"):
        sandbox.build_image(archive, "probe-review-test/rootful")
    session = Session(
        root=str(tmp_path / "s"), image="i", artifact_sha256="0" * 64, bench_version=13
    )
    (tmp_path / "s" / "src").mkdir(parents=True)
    result = tools.replay_verdict(
        session,
        {
            "outcome": "violation",
            "findings": [
                {
                    "invariant": "I4",
                    "claim": "c",
                    "probe_id": "p001-missing",
                    "violation_checks": [{"op": "http_ok"}],
                }
            ],
        },
    )
    assert not result["verified"] and "not a probe" in result["problems"][0]


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
    # Trusted in-tree fixture: building it on the test daemon is intended.
    image, _ = build_image(
        archive,
        f"probe-review-test/{abs(hash(violations)) % 10**8}",
        allow_host_daemon=True,
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
    if not expected:
        # A reviewer restarts a prepared session: read-only state is re-staged.
        session.start()
        try:
            summary = tools.run_case(
                session,
                {"user_input": "What is 2+2?"},
                {"default": {"content": "Answer: 4"}},
            )
            assert summary["http_status"] == 200, summary
        finally:
            session.stop()
