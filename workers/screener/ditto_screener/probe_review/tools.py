"""Reviewer tool surface: probe, mutate, diff, localize, and verdict replay.

Every function takes a session directory and returns JSON-serializable data, so
the same surface backs the CLI (one process per reviewer tool call) and the
in-process agent loop. Outputs are bounded: a reviewer sees compact summaries by
default and asks for a specific transcript entry when it needs more.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import secrets
import tarfile
import uuid
from pathlib import Path
from typing import Any

from ditto_screener.probe_review.battery import fingerprint
from ditto_screener.probe_review.checks import evaluate_all, executions, served_answer
from ditto_screener.probe_review.sandbox import ProbeRecord, Session

_MAX_FILE_LINES = 400
_MAX_SEARCH_HITS = 60


def load_probe(session: Session, probe_id: str) -> ProbeRecord:
    return ProbeRecord.from_json(
        json.loads((session.probes_dir / f"{probe_id}.json").read_text())
    )


def _clip(text: str, limit: int) -> str:
    return (
        text
        if len(text) <= limit
        else text[:limit] + f"...[+{len(text) - limit} chars]"
    )


def _message_text(request: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content)
        extra = ""
        if message.get("tool_calls"):
            extra = " tool_calls=" + json.dumps(message["tool_calls"])
        out.append({"role": str(message.get("role")), "content": content + extra})
    if not out and request.get("input") is not None:
        out.append({"role": "input", "content": json.dumps(request.get("input"))})
    return out


def summarize(record: ProbeRecord, *, prompt_chars: int = 600) -> dict[str, Any]:
    response = (
        record.response
        if isinstance(record.response, dict)
        else {"raw": record.response}
    )
    model = []
    for index, entry in enumerate(record.model_requests):
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        messages = _message_text(request)
        reply = entry.get("reply") if isinstance(entry.get("reply"), dict) else {}
        choice = (
            (reply.get("choices") or [{}])[0]
            if isinstance(reply.get("choices"), list)
            else {}
        )
        model.append(
            {
                "i": index,
                "tools": [
                    (t.get("function") or t).get("name")
                    for t in request.get("tools") or []
                    if isinstance(t, dict)
                ],
                "tool_choice": request.get("tool_choice"),
                "n_messages": len(messages),
                "last_message": _clip(messages[-1]["content"], prompt_chars)
                if messages
                else "",
                "reply": _clip(
                    json.dumps(choice.get("message") or reply.get("output") or reply),
                    300,
                ),
            }
        )
    return {
        "probe_id": record.probe_id,
        "label": record.label,
        "http_status": record.http_status,
        "elapsed_ms": record.elapsed_ms,
        "answer": response.get("answer"),
        "abstain": response.get("abstain"),
        "final_text": _clip(str(response.get("final_text", "")), 500),
        "reported_tool_calls": response.get("tool_calls"),
        "blocked_tool_calls": response.get("blocked_tool_calls"),
        "executions": executions(record),
        "model_calls": model,
        "error": record.error,
    }


def run_case(
    session: Session,
    request: dict[str, Any],
    script: dict[str, Any] | None = None,
    *,
    path: str = "/run",
    label: str = "",
    seed: dict[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Drive /run with a dictated model script; optionally /seed first."""
    request = dict(request)
    request.setdefault("case_id", "c" + secrets.token_hex(8))
    request.setdefault("bench_version", session.bench_version)
    request.setdefault("system_prompt", "You are a helpful assistant.")
    request.setdefault("tools", [])
    request.setdefault("user_id", f"u-{secrets.token_hex(8)}")
    if request["tools"] and not request.get("tool_endpoint"):
        request["tool_endpoint"] = session.tool_endpoint(
            request["case_id"], request["user_id"]
        )
    seed_summary = None
    if seed is not None:
        seed = dict(seed)
        seed.setdefault("user_id", request["user_id"])
        seed.setdefault("wave", 0)
        seed.setdefault("subjects", [])
        seed.setdefault("links", [])
        seeded = session.probe(seed, path="/seed", label=f"{label}:seed")
        seed_summary = {
            "probe_id": seeded.probe_id,
            "http_status": seeded.http_status,
            "response": seeded.response,
        }
    record = session.probe(request, script=script, path=path, label=label)
    summary = summarize(record)
    if seed_summary is not None:
        summary["seed"] = seed_summary
    if checks:
        summary["checks"] = evaluate_all(record, checks)
    return summary


def show(
    session: Session, probe_id: str, *, entry: int | None = None, chars: int = 4000
) -> dict[str, Any]:
    """Full model-request messages for one probe (or one model call)."""
    record = load_probe(session, probe_id)
    calls = record.model_requests
    picked = [calls[entry]] if entry is not None and 0 <= entry < len(calls) else calls
    out = []
    budget = chars
    for item in picked:
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        messages = []
        for message in _message_text(request):
            text = _clip(
                message["content"], max(200, budget // max(1, len(picked) * 3))
            )
            budget -= len(text)
            messages.append({"role": message["role"], "content": text})
        out.append(
            {
                "seq": item.get("seq"),
                "surface": item.get("surface"),
                "params": {
                    k: request.get(k)
                    for k in (
                        "model",
                        "temperature",
                        "max_tokens",
                        "tool_choice",
                        "response_format",
                    )
                    if k in request
                },
                "messages": messages,
                "reply": _clip(json.dumps(item.get("reply")), 800),
            }
        )
    return {
        "probe_id": probe_id,
        "request": record.request,
        "script": record.script,
        "model_requests": out,
        "tool_executions": [e.get("request") for e in record.tool_executions],
    }


def batch(session: Session, plan: dict[str, Any]) -> dict[str, Any]:
    """Code-mode: many probes in one call, with ``foreach`` template expansion.

    ``plan = {"foreach": [{"x": ...}, ...]?, "probes": [{"label", "request",
    "script", "seed"?, "path"?, "checks"?}]}``. ``{{x}}`` placeholders in any
    string are replaced per foreach binding. Returns compact per-probe results.
    """
    bindings = plan.get("foreach") or [{}]
    results = []
    for binding in bindings[:24]:
        for spec in (plan.get("probes") or [])[:12]:
            spec = _substitute(copy.deepcopy(spec), binding)
            summary = run_case(
                session,
                spec.get("request") or {},
                spec.get("script"),
                path=spec.get("path", "/run"),
                label=spec.get("label", ""),
                seed=spec.get("seed"),
                checks=spec.get("checks"),
            )
            results.append(
                {
                    "binding": binding,
                    "probe_id": summary["probe_id"],
                    "label": summary["label"],
                    "http_status": summary["http_status"],
                    "answer": summary["answer"],
                    "final_text": _clip(str(summary["final_text"]), 160),
                    "executions": summary["executions"],
                    "model_calls": len(summary["model_calls"]),
                    "checks": summary.get("checks"),
                }
            )
    return {"results": results}


def _substitute(value: Any, binding: dict[str, Any]) -> Any:
    if isinstance(value, str):
        for key, replacement in binding.items():
            value = value.replace("{{" + key + "}}", str(replacement))
        return value
    if isinstance(value, list):
        return [_substitute(v, binding) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, binding) for k, v in value.items()}
    return value


_TRANSFORMS = (
    "paraphrase",
    "rename_tools",
    "reorder_tools",
    "case_id_format",
    "bench_version",
    "system_prompt",
    "sign_flip",
    "drop_user_id",
    "set",
)


def mutate(
    session: Session,
    probe_id: str,
    transform: str,
    value: Any = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Re-run a probe under one metamorphic transform and diff the outcomes."""
    if transform not in _TRANSFORMS:
        raise ValueError(f"transform must be one of {_TRANSFORMS}")
    base = load_probe(session, probe_id)
    request = copy.deepcopy(base.request)
    script = copy.deepcopy(base.script)
    request["case_id"] = "c" + secrets.token_hex(8)
    if transform == "paraphrase":
        request["user_input"] = str(value)
    elif transform == "system_prompt":
        request["system_prompt"] = str(value)
    elif transform == "bench_version":
        request["bench_version"] = int(value)
    elif transform == "case_id_format":
        request["case_id"] = str(value) if value else str(uuid.uuid4())
    elif transform == "drop_user_id":
        request.pop("user_id", None)
    elif transform == "reorder_tools":
        request["tools"] = list(reversed(request.get("tools") or []))
    elif transform == "rename_tools":
        mapping = {
            t["name"]: f"{t['name']}_{value or 'v2'}"
            for t in request.get("tools") or []
        }
        for tool in request.get("tools") or []:
            tool["name"] = mapping[tool["name"]]
        text = json.dumps(script)
        for old, new in mapping.items():
            text = text.replace(f'"name": "{old}"', f'"name": "{new}"')
        script = json.loads(text)
    elif transform == "sign_flip":
        text = json.dumps(script)
        text = (
            re.sub(r"(?<![\w.-])(\d+(?:\.\d+)?)", r"-\1", text)
            if not value
            else text.replace(str(value[0]), str(value[1]))
        )
        script = json.loads(text)
    elif transform == "set":
        if not path:
            raise ValueError("set requires path")
        request[path] = value
    if request.get("tools"):
        request["tool_endpoint"] = session.tool_endpoint(
            request["case_id"], str(request.get("user_id", ""))
        )
    record = session.probe(
        request, script=script, label=f"mutate:{transform}:{probe_id}"
    )
    return {"mutated": summarize(record), "diff": diff_records(base, record)}


def diff_records(a: ProbeRecord, b: ProbeRecord) -> dict[str, Any]:
    def view(r: ProbeRecord) -> dict[str, Any]:
        response = r.response if isinstance(r.response, dict) else {}
        return {
            "http_status": r.http_status,
            "answer": served_answer(r),
            "abstain": response.get("abstain"),
            "reported_tool_calls": [
                c.get("name")
                for c in response.get("tool_calls") or []
                if isinstance(c, dict)
            ],
            "executions": executions(r),
            "model_calls": len(r.model_requests),
            "fingerprint": fingerprint(r),
        }

    left, right = view(a), view(b)
    return {
        "a": a.probe_id,
        "b": b.probe_id,
        "changed": {
            k: {"a": left[k], "b": right[k]} for k in left if left[k] != right[k]
        },
        "same": [k for k in left if left[k] == right[k]],
    }


def diff(session: Session, probe_a: str, probe_b: str) -> dict[str, Any]:
    return diff_records(load_probe(session, probe_a), load_probe(session, probe_b))


# -- source localization -------------------------------------------------------


def extract_source(archive: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                continue
            name = member.name.removeprefix("./")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(member)
            if handle is not None:
                target.write_bytes(handle.read())
                count += 1
    return count


def read_file(
    session: Session, path: str, start: int = 1, end: int | None = None
) -> dict[str, Any]:
    root = (Path(session.root) / "src").resolve()
    target = (root / path).resolve()
    if root not in target.parents:
        raise ValueError("path escapes the archive")
    lines = target.read_text(errors="replace").splitlines()
    end = min(
        len(lines), end or start + _MAX_FILE_LINES - 1, start + _MAX_FILE_LINES - 1
    )
    return {
        "path": path,
        "total_lines": len(lines),
        "start": start,
        "end": end,
        "text": "\n".join(
            f"{n}: {lines[n - 1]}" for n in range(max(1, start), end + 1)
        ),
    }


def search(session: Session, pattern: str, glob: str = "*") -> dict[str, Any]:
    root = Path(session.root) / "src"
    regex = re.compile(pattern)
    hits = []
    for file in sorted(root.rglob("*")):
        rel = str(file.relative_to(root))
        if not file.is_file() or not fnmatch.fnmatch(rel, glob):
            continue
        try:
            text = file.read_text(errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{number}: {_clip(line.strip(), 200)}")
                if len(hits) >= _MAX_SEARCH_HITS:
                    return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


# -- verdict and deterministic replay ----------------------------------------------


def validate_verdict(verdict: dict[str, Any]) -> list[str]:
    problems = []
    if verdict.get("outcome") not in {"violation", "no_violation_found"}:
        problems.append("outcome must be violation or no_violation_found")
    findings = verdict.get("findings") or []
    if verdict.get("outcome") == "violation" and not findings:
        problems.append("a violation outcome needs at least one finding")
    for index, finding in enumerate(findings):
        for key in ("invariant", "claim", "probe_id", "violation_checks"):
            if not finding.get(key):
                problems.append(f"finding {index} lacks {key}")
        if not re.fullmatch(r"I[1-8](/I[1-8])*", str(finding.get("invariant", ""))):
            problems.append(f"finding {index} invariant must be I1-I8")
    return problems


def check_citations(session: Session, verdict: dict[str, Any]) -> list[str]:
    """Every ``path:line`` citation must name an existing line of the archive.

    Replay proves behavior, not localization; this keeps a verdict from citing
    lines that do not exist. It cannot prove the cited line is the mechanism.
    """
    root = (Path(session.root) / "src").resolve()
    problems = []
    for finding in verdict.get("findings") or []:
        for citation in finding.get("source_citations") or []:
            path, _, line = str(citation).rpartition(":")
            target = (root / path).resolve()
            if (
                not path
                or not line.isdigit()
                or root not in target.parents
                or not target.is_file()
            ):
                problems.append(
                    f"citation {citation!r} does not name an archive file:line"
                )
                continue
            total = len(target.read_text(errors="replace").splitlines())
            if not 1 <= int(line) <= total:
                problems.append(
                    f"citation {citation!r} is past end of file ({total} lines)"
                )
    return problems


def replay_verdict(
    session: Session, verdict: dict[str, Any], *, replays: int = 1
) -> dict[str, Any]:
    """Re-execute every cited stimulus on a fresh harness and re-check predicates.

    A finding is ``verified`` only when its violation predicates hold on the
    original record AND on every replay, and any cited control's predicates
    hold too. No model is consulted.
    """
    problems = validate_verdict(verdict) + check_citations(session, verdict)
    if problems:
        return {"verified": False, "problems": problems, "findings": []}
    results = []
    for finding in verdict.get("findings") or []:
        original = load_probe(session, finding["probe_id"])
        checks = finding["violation_checks"]
        observations = [evaluate_all(original, checks)]
        control_obs = []
        for _ in range(replays):
            session.restart_harness()
            again = session.probe(
                original.request,
                script=original.script,
                path=original.path,
                label=f"replay:{original.probe_id}",
            )
            observations.append(evaluate_all(again, checks))
            if finding.get("control_probe_id"):
                control = load_probe(session, finding["control_probe_id"])
                replayed = session.probe(
                    control.request,
                    script=control.script,
                    path=control.path,
                    label=f"replay:{control.probe_id}",
                )
                control_obs.append(
                    evaluate_all(
                        replayed, finding.get("control_checks") or [{"op": "http_ok"}]
                    )
                )
        held = all(all(r["holds"] for r in obs) for obs in observations)
        control_ok = all(all(r["holds"] for r in obs) for obs in control_obs)
        results.append(
            {
                "invariant": finding["invariant"],
                "probe_id": finding["probe_id"],
                "stimulus_sha256": original.digest(),
                "verified": held and control_ok,
                "observations": [[r["observed"] for r in obs] for obs in observations],
            }
        )
    verified = verdict["outcome"] == "violation" and any(r["verified"] for r in results)
    return {"verified": verified, "problems": [], "findings": results}
