"""Reviewer tool surface: probe, mutate, diff, localize, and verdict replay.

Every function takes a session directory and returns JSON-serializable data, so
the same surface backs the CLI (one process per reviewer tool call) and the
in-process agent loop. Outputs are bounded: a reviewer sees compact summaries by
default and asks for a specific transcript entry when it needs more.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

from ditto_screener.probe_review.battery import fingerprint
from ditto_screener.probe_review.checks import (
    as_dict,
    evaluate_all,
    executions,
    served_answer,
)
from ditto_screener.probe_review.sandbox import ProbeRecord, Session

_MAX_FILE_LINES = 400
_MAX_SEARCH_HITS = 60
_MAX_EXTRACT_BYTES = 256 * 1024 * 1024
_MAX_EXTRACT_MEMBERS = 100_000


# Only dictated model or tool output is flipped; rule metadata such as
# ``max_uses``, ``turn`` or ``http_status`` keeps its value.
_REPLY_TEXT_KEYS = frozenset({"content", "arguments", "result"})
_SIGNED_NUMBER = re.compile(r"(?<![\w.])(-?)(\d+(?:\.\d+)?)")


def _flip_reply_numbers(script: Any, value: Any = None) -> Any:
    """Negate numbers inside reply text; ``value`` = [old, new] replaces a token."""

    def flip_text(text: str) -> str:
        if value:
            return text.replace(str(value[0]), str(value[1]))
        return _SIGNED_NUMBER.sub(
            lambda m: m.group(2) if m.group(1) else f"-{m.group(2)}", text
        )

    def walk(item: Any, key: str | None = None) -> Any:
        if isinstance(item, dict):
            return {k: walk(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v, key) for v in item]
        if isinstance(item, str) and key in _REPLY_TEXT_KEYS:
            return flip_text(item)
        return item

    return walk(script)


def _rename_tools(request: dict[str, Any], script: Any, suffix: str) -> Any:
    """Suffix every offered tool name in ``request`` (in place) and ``script``.

    Tools may be flat or OpenAI-nested (``{"type": "function", "function"}``).
    """
    targets = [
        as_dict(tool.get("function")) or tool
        for tool in request.get("tools") or []
        if isinstance(tool, dict)
    ]
    mapping = {
        name: f"{name}{suffix}"
        for target in targets
        if isinstance(name := target.get("name"), str)
    }
    for target in targets:
        if target.get("name") in mapping:
            target["name"] = mapping[target["name"]]
    text = json.dumps(script)
    if mapping:
        # One pass, so a generated name (foo -> foo_v2) is never renamed again.
        replacements = {
            f'"name": "{old}"': f'"name": "{new}"' for old, new in mapping.items()
        }
        pattern = re.compile("|".join(re.escape(key) for key in replacements))
        text = pattern.sub(lambda match: replacements[match.group(0)], text)
    return json.loads(text)


def load_probe(session: Session, probe_id: str) -> ProbeRecord:
    probes_dir = session.probes_dir.resolve()
    path = (probes_dir / f"{probe_id}.json").resolve()
    if path.parent != probes_dir:
        raise ValueError(f"invalid probe id: {probe_id!r}")
    return ProbeRecord.from_json(json.loads(path.read_text()))


def _bounded_json(value: Any, limit: int) -> Any:
    """``value`` itself when it serializes within ``limit``, else a clipped string."""
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else _clip(text, limit)


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
        request = as_dict(entry.get("request"))
        messages = _message_text(request)
        reply = as_dict(entry.get("reply"))
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
        # Collections are bounded so one high-hop probe cannot flood the
        # reviewer's context.
        "reported_tool_calls": _bounded_json(response.get("tool_calls"), 4000),
        "blocked_tool_calls": _bounded_json(response.get("blocked_tool_calls"), 4000),
        "executions": _bounded_json(executions(record), 8000),
        "model_calls": _bounded_json(model[:24], 12000),
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
    record = session.probe(
        request,
        script=script,
        path=path,
        label=label,
        preconditions=[{"path": "/seed", "request": seed}] if seed else None,
    )
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
    # One budget for everything returned: raw fields take a quarter each at
    # most, and the model calls share whatever remains.
    raw = {
        "request": _bounded_json(record.request, chars // 4),
        "script": _bounded_json(record.script, chars // 4),
        "tool_executions": _bounded_json(
            [e.get("request") for e in record.tool_executions], chars // 4
        ),
    }
    budget = chars - sum(len(json.dumps(v, default=str)) for v in raw.values())
    out = []
    for item in picked:
        request = as_dict(item.get("request"))
        messages = []
        for message in _message_text(request):
            if budget <= 0:
                messages.append({"role": message["role"], "content": "[omitted]"})
                continue
            text = _clip(
                message["content"],
                min(budget, max(200, budget // max(1, len(picked) * 3))),
            )
            budget -= len(text)
            messages.append({"role": message["role"], "content": text})
        # Replies are charged to the same budget as the messages.
        reply = _clip(json.dumps(item.get("reply")), max(0, min(800, budget)))
        budget -= len(reply)
        out.append(
            {
                "seq": item.get("seq"),
                "surface": item.get("surface"),
                "params": _bounded_json(
                    {
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
                    max(200, budget),
                ),
                "messages": messages,
                "reply": reply,
            }
        )
    return {
        "probe_id": probe_id,
        "request": raw["request"],
        "script": raw["script"],
        "model_requests": out,
        "tool_executions": raw["tool_executions"],
    }


def batch(
    session: Session, plan: dict[str, Any], *, max_probes: int | None = None
) -> dict[str, Any]:
    """Code-mode: many probes in one call, with ``foreach`` template expansion.

    ``plan = {"foreach": [{"x": ...}, ...]?, "probes": [{"label", "request",
    "script", "seed"?, "path"?, "checks"?}]}``. ``{{x}}`` placeholders in any
    string are replaced per foreach binding. Returns compact per-probe results.
    """
    bindings = plan.get("foreach") or [{}]
    results = []
    first_probe = session.probe_count
    truncated = False
    for binding in bindings[:24]:
        for spec in (plan.get("probes") or [])[:12]:
            # A seeded case costs two probes (/seed then /run).
            cost = 2 if spec.get("seed") is not None else 1
            used = session.probe_count - first_probe
            if max_probes is not None and used + cost > max_probes:
                truncated = True
                break
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
        if truncated:
            break
    # The fixed caps truncate too, not only the probe allowance.
    capped = len(bindings) > 24 or len(plan.get("probes") or []) > 12
    return {"results": results, "truncated": truncated or capped}


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
    # The harness restarts below, so the base case id can be kept and the
    # requested transform is the only difference (case_id_format changes it).
    request.setdefault("case_id", "c" + secrets.token_hex(8))
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
        script = _rename_tools(request, script, f"_{value or 'v2'}")
    elif transform == "sign_flip":
        script = _flip_reply_numbers(script, value)
    elif transform == "set":
        if not path:
            raise ValueError("set requires path")
        request[path] = value
    if request.get("tools") and "user_id" in request:
        # Rebind to the (possibly new) case; drop_user_id keeps the original.
        request["tool_endpoint"] = session.tool_endpoint(
            request["case_id"], str(request["user_id"])
        )
    # Same starting conditions as the base: fresh harness plus its setup.
    setup = _setup_for(session, base)
    session.restart_harness()
    for step in setup:
        session.probe(
            step["request"], path=step["path"], label=f"mutate-setup:{probe_id}"
        )
    record = session.probe(
        request,
        script=script,
        path=base.path,
        label=f"mutate:{transform}:{probe_id}",
        preconditions=setup,
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
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        for index, member in enumerate(tar, 1):
            if index > _MAX_EXTRACT_MEMBERS:
                raise ValueError(
                    f"source archive exceeds {_MAX_EXTRACT_MEMBERS} members"
                )
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                continue
            total += member.size
            if total > _MAX_EXTRACT_BYTES:
                raise ValueError(
                    f"source archive exceeds {_MAX_EXTRACT_BYTES} extracted bytes"
                )
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


_SEARCH_TIMEOUT_S = 20.0
# Runs in a child interpreter so a catastrophic-backtracking pattern can be
# killed at a hard deadline instead of blocking the reviewer process.
_SEARCH_SCRIPT = """
import fnmatch, json, re, sys
from pathlib import Path
root, pattern, glob = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
limit = int(sys.argv[4])
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
            line = line.strip()
            hits.append(f"{rel}:{number}: {line[:200]}")
            if len(hits) >= limit:
                print(json.dumps({"hits": hits, "truncated": True}))
                sys.exit(0)
print(json.dumps({"hits": hits, "truncated": False}))
"""


def search(session: Session, pattern: str, glob: str = "*") -> dict[str, Any]:
    re.compile(pattern)  # surface a malformed pattern as an ordinary error
    root = Path(session.root) / "src"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _SEARCH_SCRIPT,
                str(root),
                pattern,
                glob,
                str(_MAX_SEARCH_HITS),
            ],
            capture_output=True,
            text=True,
            timeout=_SEARCH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "hits": [],
            "truncated": True,
            "error": f"search exceeded {_SEARCH_TIMEOUT_S:g}s; simplify the pattern",
        }
    if completed.returncode != 0:
        return {"hits": [], "truncated": False, "error": completed.stderr[-300:]}
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


# -- verdict and deterministic replay ----------------------------------------------


def validate_verdict(verdict: dict[str, Any]) -> list[str]:
    problems = []
    if verdict.get("outcome") not in {"violation", "no_violation_found"}:
        problems.append("outcome must be violation or no_violation_found")
    findings = verdict.get("findings") or []
    if not isinstance(findings, list):
        return [*problems, "findings must be a list"]
    if verdict.get("outcome") == "violation" and not findings:
        problems.append("a violation outcome needs at least one finding")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            problems.append(f"finding {index} must be an object")
            continue
        checks = finding.get("violation_checks")
        if checks and (
            not isinstance(checks, list)
            or not all(isinstance(check, dict) for check in checks)
        ):
            problems.append(f"finding {index} violation_checks must be objects")
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
    for index, finding in enumerate(verdict.get("findings") or []):
        if not isinstance(finding, dict):
            continue  # validate_verdict reports the malformed finding
        citations = finding.get("source_citations") or []
        if not isinstance(citations, list) or not all(
            isinstance(citation, str) for citation in citations
        ):
            problems.append(f"finding {index} source_citations must be strings")
            continue
        for citation in citations:
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


def _probe_number(probe_id: str) -> int:
    """The sequence number in ``p<n>-<hex>``; unparseable ids sort last."""
    head = probe_id[1:].split("-", 1)[0]
    return int(head) if head.isdigit() else 1 << 62


def _setup_for(session: Session, record: ProbeRecord) -> list[dict[str, Any]]:
    """Preconditions to re-post before ``record`` on a fresh harness.

    Explicit preconditions win; otherwise every earlier ``/seed`` probe in the
    session for the same user is re-posted in order, so a finding that relied
    on a separately run seed still reproduces.
    """
    if record.preconditions:
        return record.preconditions
    user = record.request.get("user_id")
    target = _probe_number(record.probe_id)
    setup = []
    # Numeric order: p1000-… must follow p999-… (lexical order would not).
    for path in sorted(
        session.probes_dir.glob("p*.json"), key=lambda p: _probe_number(p.stem)
    ):
        earlier = ProbeRecord.from_json(json.loads(path.read_text()))
        if _probe_number(earlier.probe_id) >= target:
            break
        if earlier.path == "/seed" and user and earlier.request.get("user_id") == user:
            setup.append({"path": "/seed", "request": earlier.request})
    return setup


def _replay_one(session: Session, record: ProbeRecord) -> ProbeRecord:
    for step in _setup_for(session, record):
        session.probe(
            step["request"], path=step["path"], label=f"replay-setup:{record.probe_id}"
        )
    return session.probe(
        record.request,
        script=record.script,
        path=record.path,
        label=f"replay:{record.probe_id}",
    )


def replay_verdict(
    session: Session, verdict: dict[str, Any], *, replays: int = 1
) -> dict[str, Any]:
    """Re-execute every cited stimulus on a fresh harness and re-check predicates.

    A finding is ``verified`` only when its violation predicates hold on the
    original record AND on every replay, and any cited control's predicates
    hold too. No model is consulted.
    """
    if replays < 1:
        return {
            "verified": False,
            "problems": ["replays must be at least 1"],
            "findings": [],
        }
    problems = validate_verdict(verdict) + check_citations(session, verdict)
    if problems:
        return {"verified": False, "problems": problems, "findings": []}
    results = []
    for finding in verdict.get("findings") or []:
        original = load_probe(session, finding["probe_id"])
        checks = finding["violation_checks"]
        observations = [evaluate_all(original, checks)]
        control_obs = []
        if finding.get("control_probe_id"):
            # The control must hold as cited, not only on replay.
            cited = load_probe(session, finding["control_probe_id"])
            control_obs.append(
                evaluate_all(
                    cited, finding.get("control_checks") or [{"op": "http_ok"}]
                )
            )
        for _ in range(replays):
            session.restart_harness()
            again = _replay_one(session, original)
            observations.append(evaluate_all(again, checks))
            if finding.get("control_probe_id"):
                control = load_probe(session, finding["control_probe_id"])
                # Isolate the control from the finding replay's state.
                session.restart_harness()
                replayed = _replay_one(session, control)
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
