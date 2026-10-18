"""Minimal scorer-shaped harness used to calibrate the probe reviewer.

The honest default relays the model faithfully. ``PROBE_FIXTURE_VIOLATIONS``
(comma-separated, set at build time) switches on known-bad mechanisms copied
from adjudicated v13 rejections, so the battery's sensitivity and specificity
are testable without miner artifacts.
"""

import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("OPENAI_BASE_URL", "http://localhost:11435/v1").rstrip("/")
MODEL = os.environ.get("DITTOBENCH_MODEL", "qwen/qwen3-32b")
KEY = os.environ.get("OPENAI_API_KEY", "x")
VIOLATIONS = set(
    filter(None, os.environ.get("PROBE_FIXTURE_VIOLATIONS", "").split(","))
)
MEMORY = {}


def post(url, payload, timeout=60):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def chat(messages, tools, tool_choice=None):
    payload = {"model": MODEL, "messages": messages}
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    return post(f"{BASE}/chat/completions", payload)["choices"][0]["message"]


def extract_answer(text):
    match = re.search(r"Answer:\s*(.+)", text or "")
    return match.group(1).strip() if match else (text or "").strip()


def run(request):
    tools = request.get("tools") or []
    memories = MEMORY.get(request.get("user_id") or "", [])
    context = "\n".join(f"- {p['prompt']} -> {p['response']}" for p in memories)
    messages = [
        {"role": "system", "content": request.get("system_prompt", "")},
        {
            "role": "user",
            "content": request["user_input"]
            + (f"\n\nMemory:\n{context}" if context else ""),
        },
    ]
    observed, executed = [], set()
    reply = {}
    for hop in range(8):
        choice = None
        if "pin" in VIOLATIONS and tools and hop == 0:
            choice = {"type": "function", "function": {"name": tools[0]["name"]}}
        reply = chat(messages, tools, choice)
        calls = reply.get("tool_calls") or []
        if not calls:
            break
        messages.append(
            {"role": "assistant", "content": reply.get("content"), "tool_calls": calls}
        )
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except ValueError:
                if "malformed_empty" not in VIOLATIONS:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "error: arguments were not valid JSON",
                        }
                    )
                    continue
                args = {}
            key = (name, json.dumps(args, sort_keys=True))
            if "dedupe" in VIOLATIONS and key in executed:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "skipped duplicate",
                    }
                )
                continue
            if "cap3" in VIOLATIONS and len(observed) >= 3:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "call limit reached",
                    }
                )
                continue
            result = {"result": "", "error": "no endpoint"}
            if request.get("tool_endpoint"):
                try:
                    result = post(
                        request["tool_endpoint"],
                        {
                            "case_id": request["case_id"],
                            "user_id": request.get("user_id", ""),
                            "name": name,
                            "args": args,
                            "hop": hop,
                        },
                    )
                except Exception as error:  # noqa: BLE001
                    result = {"result": "", "error": type(error).__name__}
                    if "report_failed" not in VIOLATIONS:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": "error: " + result["error"],
                            }
                        )
                        continue
            executed.add(key)
            observed.append({"name": name, "args": args, "hop": hop})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result.get("result") or result.get("error", ""),
                }
            )
    text = reply.get("content") or ""
    answer = extract_answer(text)
    if "abstain_regen" in VIOLATIONS and "know" in answer.lower():
        messages.append({"role": "user", "content": "Please give a definite answer."})
        text = chat(messages, [])["content"] or ""
        answer = extract_answer(text)
    if "sign" in VIOLATIONS:
        answer = answer.lstrip("-")
    return {
        "final_text": text,
        "answer": answer,
        "tool_calls": observed,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200 if self.path == "/health" else 404, {"status": "ok"})

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        )
        if self.path == "/seed":
            MEMORY.setdefault(body.get("user_id", ""), []).extend(
                body.get("pairs") or []
            )
            self._send(
                200, {"pairs": len(body.get("pairs") or []), "subjects": 0, "links": 0}
            )
        elif self.path == "/run":
            self._send(200, run(body))
        else:
            self._send(404, {})

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
