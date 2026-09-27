"""Minimal V13 source-review control: full user records, model decisions, faithful tools.

This is a calibration artifact, not a miner baseline. It intentionally has no
opaque model, answer template, retrieval selector, or benchmark-specific policy.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_records: dict[str, dict[str, dict]] = {}
_subjects: dict[str, dict[str, dict]] = {}
_links: dict[str, dict[tuple[str, str], dict]] = {}
_lock = threading.Lock()
_MAX_BODY = 32 * 1024 * 1024
_MEMORY_TOOLS = {
    "save_memory",
    "search_memories",
    "fetch_memories",
    "search_subjects",
    "search_memories_in_subjects",
    "update_memory",
    "delete_memory",
}


def _post_json(url: str, payload: dict, *, broker: bool = False) -> dict:
    headers = {"Content-Type": "application/json"}
    if broker:
        headers["Authorization"] = "Bearer ticket"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("upstream response is not an object")
    return result


def _user_id(request: dict) -> str:
    value = request.get("user_id")
    return value if isinstance(value, str) and value else "miner"


def seed(request: dict) -> dict:
    pairs = request.get("pairs", [])
    subjects = request.get("subjects", [])
    links = request.get("links", [])
    if not all(isinstance(items, list) for items in (pairs, subjects, links)):
        raise ValueError("seed collections must be lists")
    user_id = _user_id(request)
    prepared_pairs = {}
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get("pair_id"), str):
            raise ValueError("invalid pair")
        prepared_pairs[pair["pair_id"]] = pair
    prepared_subjects = {}
    for subject in subjects:
        if not isinstance(subject, dict) or not isinstance(subject.get("id"), str):
            raise ValueError("invalid subject")
        prepared_subjects[subject["id"]] = subject
    prepared_links = {}
    for link in links:
        if not isinstance(link, dict) or not all(
            isinstance(link.get(key), str) for key in ("subject_id", "pair_id")
        ):
            raise ValueError("invalid link")
        prepared_links[(link["subject_id"], link["pair_id"])] = link
    with _lock:
        _records.setdefault(user_id, {}).update(prepared_pairs)
        _subjects.setdefault(user_id, {}).update(prepared_subjects)
        _links.setdefault(user_id, {}).update(prepared_links)
    return {"pairs": len(pairs), "subjects": len(subjects), "links": len(links)}


def _local_memory_tool(user_id: str, name: str, args: dict) -> dict:
    """Execute a model-selected memory call against only this user's records."""
    with _lock:
        pairs = _records.setdefault(user_id, {})
        subjects = _subjects.setdefault(user_id, {})
        links = _links.setdefault(user_id, {})
        if name == "fetch_memories":
            return {"memories": [pairs[key] for key in args.get("pairIds", []) if key in pairs]}
        if name == "save_memory":
            identifier = str(uuid.uuid4())
            pairs[identifier] = {"pair_id": identifier, "prompt": args["content"], "response": ""}
            return {"pair_id": identifier, "saved": True}
        if name == "update_memory":
            identifier = args["pair_id"]
            if identifier not in pairs:
                return {"error": "memory not found"}
            pairs[identifier] = {**pairs[identifier], "response": args["content"]}
            return {"pair_id": identifier, "updated": True}
        if name == "delete_memory":
            identifier = args["pair_id"]
            if identifier not in pairs:
                return {"error": "memory not found"}
            del pairs[identifier]
            for key in [key for key in links if key[1] == identifier]:
                del links[key]
            return {"pair_id": identifier, "deleted": True}
        queries = [str(query).casefold() for query in args.get("queries", [])]
        if name == "search_subjects":
            return {
                "subjects": [
                    item
                    for item in subjects.values()
                    if not queries or any(query in json.dumps(item).casefold() for query in queries)
                ]
            }
        scope = pairs.values()
        if name == "search_memories_in_subjects":
            ids = {pair_id for subject_id, pair_id in links if subject_id == args["subject_id"]}
            scope = [pairs[key] for key in sorted(ids) if key in pairs]
        return {
            "memories": [
                item
                for item in scope
                if not queries or any(query in json.dumps(item).casefold() for query in queries)
            ]
        }


def run(request: dict) -> dict:
    case_id = request["case_id"]
    user_id = _user_id(request)
    base_url = request["inference_base_url"]
    if not isinstance(case_id, str) or not isinstance(base_url, str) or not base_url:
        raise ValueError("invalid run identity")
    base_url = base_url.rstrip("/")
    with _lock:
        records = list(_records.get(user_id, {}).values())
        subjects = list(_subjects.get(user_id, {}).values())
        links = list(_links.get(user_id, {}).values())
    # Every current-user record is passed intact. Upstream context failures are
    # errors, never an excuse to silently remove evidence or invent an answer.
    messages = [
        {"role": "system", "content": request["system_prompt"]},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "records": records,
                    "subjects": subjects,
                    "links": links,
                    "request": request["user_input"],
                },
                ensure_ascii=False,
            ),
        },
    ]
    offered = request.get("tools", [])
    if not isinstance(offered, list):
        raise ValueError("tools must be a list")
    tools = [
        {
            "type": "function",
            "function": {
                "name": item["name"],
                "description": item.get("description", ""),
                "parameters": item["parameters"],
            },
        }
        for item in offered
    ]
    ledger = []
    input_tokens = output_tokens = 0
    started = time.monotonic()
    for _ in range(16):
        payload = {
            "model": os.environ.get("DITTOBENCH_MODEL", "openai/gpt-oss-20b"),
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        completion = _post_json(f"{base_url}/chat/completions", payload, broker=True)
        usage = completion.get("usage") or {}
        input_tokens += int(usage.get("prompt_tokens") or 0)
        output_tokens += int(usage.get("completion_tokens") or 0)
        choice = completion["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        messages.append(message)
        if not calls:
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("model returned no final text")
            return {
                "final_text": content,
                "tool_calls": ledger,
                "prompt_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        for call in calls:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            hop = len(ledger)
            if name in _MEMORY_TOOLS:
                outcome = _local_memory_tool(user_id, name, args)
            else:
                endpoint = request.get("tool_endpoint")
                if not isinstance(endpoint, str) or not endpoint:
                    raise ValueError("model selected an external tool without a tool endpoint")
                outcome = _post_json(
                    endpoint,
                    {
                        "case_id": case_id,
                        "user_id": user_id,
                        "name": name,
                        "args": args,
                        "hop": hop,
                    },
                )
            ledger.append({"name": name, "args": args, "hop": hop})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(outcome, ensure_ascii=False),
                }
            )
    raise ValueError("model turn limit reached before a final answer")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/health":
            self.send_error(404)
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= _MAX_BODY:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            if self.path == "/seed":
                result = seed(request)
            elif self.path == "/run":
                result = run(request)
            else:
                self.send_error(404)
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except (IndexError, KeyError, TypeError, ValueError, urllib.error.URLError) as error:
            body = json.dumps({"error": str(error)}).encode("utf-8")
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
