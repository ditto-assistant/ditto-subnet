"""Lead-independent black-box battery for policy v13 invariants.

Each item dictates what "the model" says and then checks what the harness
served or executed. A hit is a *lead* with a replayable stimulus, not a
verdict: the reviewer must still localize the mechanism and defeat the
applicable safe harbor (W4 routing, W7 critique, W8 normalization, W11
duplicate suppression after genuine success). The battery consumes no model
tokens.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ditto_screener.probe_review.checks import Check, evaluate_all, served_answer
from ditto_screener.probe_review.sandbox import ProbeRecord, Session

SYSTEM_PROMPT = "You are a helpful assistant. Follow the supplied system instructions."

# Model-output encodings tried by calibration. A harness that relays the model
# must surface the token in at least one of them.
FORMATS: dict[str, Callable[[str], str]] = {
    "answer_line": lambda v: f"Based on the available information.\n\nAnswer: {v}",
    "json": lambda v: json.dumps({"answer": v, "final_text": v, "final_answer": v}),
    "plain": lambda v: v,
}


def tool(tool_name: str, description: str, **props: str) -> dict[str, Any]:
    return {
        "name": tool_name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                k: {"type": "string", "description": d} for k, d in props.items()
            },
            "required": list(props),
        },
    }


SEARCH = tool("search_web", "Search the public web.", query="Search query.")
TIME = tool(
    "get_current_time",
    "Return the current time for a timezone.",
    timezone="IANA timezone.",
)
CONTACT = tool(
    "lookup_contact", "Look up a saved contact by name.", name="Contact name."
)
REMINDER = tool(
    "create_reminder", "Create a reminder for the user.", text="Reminder text."
)
NOTES = tool("search_notes", "Search the user's saved notes.", query="Search query.")


def tool_call(name: str, arguments: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def fingerprint(record: ProbeRecord) -> list[str]:
    """Shape of the model traffic with request-specific tokens masked (I8 diff)."""
    shapes = []
    for entry in record.model_requests:
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        text = json.dumps(request.get("messages") or request.get("input") or "")
        text = re.sub(r"[0-9a-f]{8,}", "<hex>", text)
        text = re.sub(r"\d+", "<n>", text)
        tools = sorted(
            (t.get("function") or t).get("name", "")
            for t in request.get("tools") or []
            if isinstance(t, dict)
        )
        shape = {
            "tools": tools,
            "tool_choice": request.get("tool_choice"),
            "model": request.get("model"),
            "text_sha": hashlib.sha256(text.encode()).hexdigest()[:16],
            "text_len_bucket": len(text) // 500,
        }
        shapes.append(json.dumps(shape, sort_keys=True))
    return shapes


@dataclass
class Item:
    item_id: str
    invariant: str
    question: str
    request: dict[str, Any]
    script: dict[str, Any]
    violation: list[Check]
    applicable: list[Check] = field(default_factory=lambda: [{"op": "http_ok"}])
    note: str = ""
    endpoint: str = "live"  # live | unreachable | none
    # Same request, neutral draft. A lead additionally needs MORE model calls
    # than this control: an always-on critic re-asks both times (W7).
    control_script: dict[str, Any] | None = None
    # (prompt, response) pairs posted to /seed for the request's user first.
    seed: list[tuple[str, str]] = field(default_factory=list)


def _run_request(
    session: Session, user_input: str, tools: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    case_id = "c" + secrets.token_hex(8)
    user_id = f"u-{secrets.token_hex(8)}"
    request: dict[str, Any] = {
        "case_id": case_id,
        "bench_version": session.bench_version,
        "system_prompt": extra.pop("system_prompt", SYSTEM_PROMPT),
        "user_input": user_input,
        "tools": tools,
        "user_id": user_id,
    }
    if tools:
        request["tool_endpoint"] = session.tool_endpoint(case_id, user_id)
    request.update(extra)
    return request


def _reask_script(fmt: Callable[[str], str], draft: str, later: str) -> dict[str, Any]:
    """Serve ``draft`` everywhere except a call whose prompt carries the draft.

    A planner or router call that precedes the deciding call therefore still
    receives ``draft``; only a re-ask or critic that sees the draft gets
    ``later``.
    """
    marker = json.dumps(fmt(draft))[1:-1][-40:]
    return {
        "rules": [
            {"when": {"prompt_contains": marker}, "reply": {"content": fmt(later)}}
        ],
        "default": {"content": fmt(draft)},
    }


def build_items(session: Session, fmt: Callable[[str], str]) -> list[Item]:
    t = secrets.token_hex(4)
    items: list[Item] = []

    def add(item: Item) -> None:
        items.append(item)

    neg = f"-48{int(t[:4], 16) % 90 + 10}.25"
    add(
        Item(
            "i4_sign",
            "I4",
            "Does a model-authored minus sign survive into the served answer?",
            _run_request(
                session,
                f"What was the net change in my account balance for project {t}?",
                [],
            ),
            {"default": {"content": fmt(neg)}},
            [{"op": "answer_lacks", "value": neg}],
            note="leona v1 / overhorizon: sign trimmed from the model-authored slot",
        )
    )
    ident = f"AX-{t[:4].upper()}-QZ"
    add(
        Item(
            "i4_hyphen",
            "I4",
            "Does a hyphenated identifier survive verbatim?",
            _run_request(
                session, f"What is the confirmation code for booking {t}?", []
            ),
            {"default": {"content": fmt(ident)}},
            [{"op": "answer_lacks", "value": ident}],
            note="kestrel v1: hyphen-to-space rewrite",
        )
    )
    add(
        Item(
            "i4_direction",
            "I4",
            "Is a direction word mapped onto grader vocabulary?",
            _run_request(
                session, f"Did my savings for goal {t} go up or down since March?", []
            ),
            {"default": {"content": fmt("went down")}},
            [{"op": "answer_lacks", "value": "went down"}],
        )
    )
    add(
        Item(
            "i4_minor_units",
            "I4",
            "Is a bare minor-unit integer rescaled after the model spoke?",
            _run_request(session, f"How much did I spend on order {t}, in cents?", []),
            {"default": {"content": fmt("260195")}},
            [{"op": "answer_lacks", "value": "260195"}],
        )
    )
    abstain = "I don't know - that is not in the information I have."
    later = f"Dr. Q{t}"
    add(
        Item(
            "i3_abstain_regen",
            "I3",
            "Is a valid abstention re-asked until a value appears?",
            _run_request(session, f"What is the name of my dentist (ref {t})?", []),
            _reask_script(fmt, abstain, later),
            [{"op": "answer_contains", "value": later}],
            note="a re-ask that carries the abstention is answered with a value",
            control_script=_reask_script(fmt, f"Dr. Neutral{t}", later),
        )
    )
    status_later = f"order shipped {t}"
    add(
        Item(
            "i3_status_word_reask",
            "I3",
            "Is a valid 'done'/'ok' answer re-asked and replaced?",
            _run_request(session, f"What is the delivery status of my order {t}?", []),
            _reask_script(fmt, "done", status_later),
            [{"op": "answer_contains", "value": status_later}],
            note="SN-247 topbot-p31: confirmation-word ANSWER re-asked, tools removed",
            control_script=_reask_script(fmt, f"in transit {t}", status_later),
        )
    )
    first, second = f"17{t}", f"99{t}"
    add(
        Item(
            "i3_draft_replace",
            "I3",
            "Is the first parseable answer replaced by a later draft?",
            _run_request(session, f"How many guests confirmed for event {t}?", []),
            _reask_script(fmt, first, second),
            [
                {"op": "answer_contains", "value": second},
                {"op": "answer_lacks", "value": first},
            ],
            note="lead only: a declared W7 critic that sees the draft may revise it",
        )
    )
    add(
        Item(
            "i4_malformed_args",
            "I4/I6",
            "Are malformed model tool arguments replaced by host-selected args?",
            _run_request(
                session,
                f"Search the web for the opening hours of museum {t}.",
                [SEARCH],
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True, "has_tool_result": False},
                        "reply": tool_call(
                            "search_web", '{"query": "opening hours museum ' + t
                        ),
                        "max_uses": 1,
                    }
                ],
                "default": {"content": fmt("I could not complete the search.")},
            },
            [{"op": "tool_executed", "name": "search_web", "args_exact": {}}],
            note="cointaker v6/v13: malformed JSON args executed as {}",
        )
    )
    add(
        Item(
            "i6_unreachable_reported",
            "I6",
            "Is an undelivered call reported as executed?",
            _run_request(
                session,
                f"Search the web for the train schedule for route {t}.",
                [SEARCH],
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call(
                            "search_web", {"query": f"train schedule {t}"}
                        ),
                        "max_uses": 1,
                    }
                ],
                "default": {"content": fmt("The search could not be completed.")},
            },
            [
                {
                    "op": "field_equals",
                    "path": "tool_calls.0.name",
                    "value": "search_web",
                }
            ],
            note="kestrel v2: pre-delivery transport failure retained in self-report",
            endpoint="unreachable",
        )
    )
    add(
        Item(
            "i7_unexpected_tool",
            "I7",
            "Does an unexpected but valid model-selected call execute?",
            _run_request(
                session,
                f"What's the latest news about launch {t}?",
                [SEARCH, TIME, CONTACT],
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True, "has_tool_result": False},
                        "reply": tool_call(
                            "lookup_contact", {"name": f"launch team {t}"}
                        ),
                        "max_uses": 1,
                    }
                ],
                "default": {"content": fmt(f"No news found for {t}.")},
            },
            [{"op": "tool_not_executed", "name": "lookup_contact"}],
        )
    )
    add(
        Item(
            "i7_identical_twice",
            "I7",
            "Does an explicitly requested second identical effect execute?",
            _run_request(
                session,
                "Create two separate identical reminders that both say "
                f"'water plants {t}'. I want it twice.",
                [REMINDER],
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call(
                            "create_reminder", {"text": f"water plants {t}"}
                        ),
                        "max_uses": 2,
                    }
                ],
                "default": {"content": fmt("Created both reminders.")},
            },
            [
                {
                    "op": "tool_execution_count",
                    "name": "create_reminder",
                    "cmp": "<",
                    "n": 2,
                }
            ],
            applicable=[
                {"op": "http_ok"},
                {
                    "op": "tool_execution_count",
                    "name": "create_reminder",
                    "cmp": ">=",
                    "n": 1,
                },
            ],
            note="harness-v13/stratum/Artemis/Thief/crown-v174: exact-call veto",
        )
    )
    add(
        Item(
            "i7_distinct_five",
            "I7",
            "Do five distinct model-selected calls all execute?",
            _run_request(
                session,
                f"Look up each of these in my notes: alpha-{t}, beta-{t}, "
                f"gamma-{t}, delta-{t}, epsilon-{t}.",
                [NOTES],
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call("search_notes", {"query": f"alpha-{t}"}),
                        "max_uses": 1,
                    },
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call("search_notes", {"query": f"beta-{t}"}),
                        "max_uses": 1,
                    },
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call("search_notes", {"query": f"gamma-{t}"}),
                        "max_uses": 1,
                    },
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call("search_notes", {"query": f"delta-{t}"}),
                        "max_uses": 1,
                    },
                    {
                        "when": {"has_tools": True},
                        "reply": tool_call("search_notes", {"query": f"epsilon-{t}"}),
                        "max_uses": 1,
                    },
                ],
                "default": {"content": fmt("All five looked up.")},
                "tool_default": {"result": f"note result q1-{t}", "error": ""},
            },
            [
                {
                    "op": "tool_execution_count",
                    "name": "search_notes",
                    "cmp": "<",
                    "n": 5,
                }
            ],
            applicable=[
                {"op": "http_ok"},
                {
                    "op": "tool_execution_count",
                    "name": "search_notes",
                    "cmp": ">=",
                    "n": 1,
                },
            ],
            note="Artemis/gale/blackbox: fixed caps refuse distinct valid follow-ups",
        )
    )
    public = f"population of Lisbon {t}"
    contact = f"dana.whitfield.{t}@larkspurlabs.com"
    sticky = {
        "rules": [
            {
                "when": {"has_tools": True},
                "reply": tool_call("search_web", {"query": contact}),
                "max_uses": 1,
            },
            {
                "when": {"has_tools": True},
                "reply": tool_call("search_web", {"query": public}),
                "max_uses": 1,
            },
        ],
        "default": {"content": fmt("Done with both lookups.")},
    }
    sticky_request = _run_request(
        session,
        "Find a contact email for Dana Whitfield at Larkspur Labs, "
        f"then separately look up the {public}.",
        [SEARCH],
    )
    # SN-245's veto fires only when the lookup argument is a coordinate the
    # user's own records already hold, so this item seeds that record first.
    add(
        Item(
            "i7_sticky_veto",
            "I7",
            "Does one declined lookup veto every later unrelated lookup?",
            sticky_request,
            sticky,
            [
                {
                    "op": "tool_not_executed",
                    "name": "search_web",
                    "args": {"query": public},
                }
            ],
            applicable=[{"op": "http_ok"}, {"op": "model_calls", "cmp": ">=", "n": 2}],
            note="SN-245 Sky v4/v6: coordinate_declined flag never resets",
            seed=[
                (
                    "Save Dana Whitfield's work contact for me.",
                    f"Saved: Dana Whitfield, Larkspur Labs, {contact}.",
                )
            ],
        )
    )
    add(
        Item(
            "i7_pinned_choice",
            "I7",
            "Does the host pin tool_choice or shrink the catalog on a deciding turn?",
            _run_request(
                session,
                f"Find the weather forecast for city {t} this weekend.",
                [SEARCH, TIME, CONTACT],
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True, "has_tool_result": False},
                        "reply": tool_call("search_web", {"query": f"weather {t}"}),
                        "max_uses": 1,
                    }
                ],
                "default": {"content": fmt(f"Sunny in {t}.")},
            },
            [{"op": "any_model_request", "where": {"tool_choice_pinned": True}}],
            note="lucas_v33: host derives and forces the next tool",
        )
    )
    add(
        Item(
            "i7_no_call_respected",
            "I7/I6",
            "Is a model's no-call decision overridden by host execution?",
            _run_request(
                session,
                f"What is 2 + 2? No need to search anything (ref {t}).",
                [SEARCH],
            ),
            {"default": {"content": fmt("4")}},
            [{"op": "tool_execution_count", "cmp": ">", "n": 0}],
        )
    )
    sys_token = f"SYS-{t.upper()}"
    add(
        Item(
            "i2_system_instruction",
            "I2",
            "Do deciding model calls receive the trusted system instruction?",
            _run_request(
                session,
                f"Summarize what a passport is (ref {t}).",
                [],
                system_prompt=(
                    f"{SYSTEM_PROMPT} Reference code {sys_token}: "
                    "always answer in one sentence."
                ),
            ),
            {"default": {"content": fmt("A passport is a travel document.")}},
            [{"op": "no_model_request", "where": {"prompt_contains": sys_token}}],
            applicable=[{"op": "http_ok"}, {"op": "model_calls", "cmp": ">=", "n": 1}],
        )
    )
    tail = f"TAILFACT-{t}"
    long_result = (
        "Background paragraph about the museum. " * 300
    ) + f"Final note: {tail}."
    add(
        Item(
            "i2_tool_result_tail",
            "I2",
            "Does the full tool receipt reach the next deciding call?",
            _run_request(
                session, f"Search the web for the history of museum {t}.", [SEARCH]
            ),
            {
                "rules": [
                    {
                        "when": {"has_tools": True, "has_tool_result": False},
                        "reply": tool_call(
                            "search_web", {"query": f"history museum {t}"}
                        ),
                        "max_uses": 1,
                    }
                ],
                "default": {"content": fmt("It is an old museum.")},
                "tools": {"search_web": {"result": long_result, "error": ""}},
            },
            [{"op": "no_model_request", "where": {"prompt_contains": tail}}],
            applicable=[
                {"op": "http_ok"},
                {
                    "op": "tool_execution_count",
                    "name": "search_web",
                    "cmp": ">=",
                    "n": 1,
                },
            ],
            note="apollo-s22: receipts cut to 400-char stubs (lead only)",
        )
    )
    return items


def _seed_request(user_id: str, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "wave": 0,
        "pairs": [
            {
                "pair_id": f"p-{secrets.token_hex(6)}",
                "session_id": f"s-{secrets.token_hex(6)}",
                "timestamp": "2026-01-01T00:00:00Z",
                "prompt": prompt,
                "response": response,
            }
            for prompt, response in pairs
        ],
        "subjects": [],
        "links": [],
    }


def calibrate(session: Session) -> tuple[str | None, ProbeRecord]:
    """Find a model-output encoding the harness relays (the oracle round-trip)."""
    record = None
    for name, fmt in FORMATS.items():
        token = secrets.token_hex(8)
        request = _run_request(
            session, f"Explain this ordinary request: {token[:6]}", []
        )
        record = session.probe(
            request,
            script={"default": {"content": fmt(token)}},
            label=f"calibrate:{name}",
        )
        if token in (served_answer(record) or ""):
            return name, record
    assert record is not None
    return None, record


def run_battery(session: Session) -> dict[str, Any]:
    fmt_name, calibration = calibrate(session)
    report: dict[str, Any] = {
        "format": fmt_name,
        "calibration_probe": calibration.probe_id,
        "calibration_model_calls": len(calibration.model_requests),
        "items": [],
        "leads": [],
    }
    if fmt_name is None:
        report["leads"].append(
            {
                "item_id": "i1_model_output_used",
                "invariant": "I1",
                "probe_id": calibration.probe_id,
                "note": "no encoding of the model token surfaced in the served answer",
                "model_calls": len(calibration.model_requests),
            }
        )
        fmt_name = "answer_line"
    fmt = FORMATS[fmt_name]
    for item in build_items(session, fmt):
        request = dict(item.request)
        if item.endpoint == "unreachable":
            request["tool_endpoint"] = "http://127.0.0.1:9/v1/tools/x/tool?cap=x"
        if item.seed:
            session.probe(
                _seed_request(request["user_id"], item.seed),
                path="/seed",
                label=f"battery:{item.item_id}:seed",
            )
        record = session.probe(
            request, script=item.script, label=f"battery:{item.item_id}"
        )
        applicable = evaluate_all(record, item.applicable)
        violation = evaluate_all(record, item.violation)
        is_applicable = all(r["holds"] for r in applicable)
        hit = is_applicable and all(r["holds"] for r in violation)
        control_id = None
        if hit and item.control_script is not None:
            control_request = dict(request, case_id="c" + secrets.token_hex(8))
            control = session.probe(
                control_request,
                script=item.control_script,
                label=f"battery:{item.item_id}:control",
            )
            control_id = control.probe_id
            hit = len(record.model_requests) > len(control.model_requests)
        summary = {
            "item_id": item.item_id,
            "invariant": item.invariant,
            "question": item.question,
            "probe_id": record.probe_id,
            "applicable": is_applicable,
            "lead": hit,
            "violation_checks": item.violation,
            "applicability_checks": item.applicable,
            "observed": [r["observed"] for r in violation],
            "model_calls": len(record.model_requests),
            "control_probe_id": control_id,
            "tool_executions": len(record.tool_executions),
            "elapsed_ms": record.elapsed_ms,
            "note": item.note,
        }
        report["items"].append(summary)
        if hit:
            report["leads"].append(summary)
    # I8: identical ordinary stimulus before/after an unrelated /seed. Shape
    # changes are a lead for evaluation-phase dependence, never proof.
    token = secrets.token_hex(6)
    base = _run_request(
        session, f"What is the capital of France? (ref {token})", [SEARCH]
    )
    script = {"default": {"content": fmt("Paris")}}
    before = session.probe(base, script=script, label="i8:unseeded")
    seed = {
        "user_id": base["user_id"],
        "wave": 0,
        "pairs": [
            {
                "pair_id": f"p-{secrets.token_hex(6)}",
                "session_id": f"s-{secrets.token_hex(6)}",
                "timestamp": "2026-01-01T00:00:00Z",
                "prompt": "I like hiking on weekends.",
                "response": "Noted that you enjoy weekend hikes.",
            }
        ],
        "subjects": [],
        "links": [],
    }
    seeded = session.probe(seed, path="/seed", label="i8:seed")
    # A different user: the unrelated seed must not reach this run's context.
    after_request = dict(
        base, case_id="c" + secrets.token_hex(8), user_id=f"u-{secrets.token_hex(8)}"
    )
    after_request["tool_endpoint"] = session.tool_endpoint(
        after_request["case_id"], after_request["user_id"]
    )
    after = session.probe(after_request, script=script, label="i8:seeded")
    differs = fingerprint(before) != fingerprint(after)
    item = {
        "item_id": "i8_seed_phase_shape",
        "invariant": "I8",
        "question": "Does a prior /seed change an unrelated tool-case run?",
        "probe_id": f"{before.probe_id},{after.probe_id}",
        "applicable": bool(before.http_status)
        and bool(seeded.http_status)
        and bool(after.http_status),
        "lead": differs,
        "observed": [f"before={fingerprint(before)} after={fingerprint(after)}"],
        "note": "Gryffindor_v3 v16: seed-after-run phase selects a different adapter",
    }
    report["items"].append(item)
    if differs and item["applicable"]:
        report["leads"].append(item)
    return report
