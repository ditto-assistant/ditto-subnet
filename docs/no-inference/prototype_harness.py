# ruff: noqa: E501  (measurement prototype: long prompt strings)
"""Baseline NO-CHAT DittoBench harness prototype (decision lane + lexical retrieval).

Measurement prototype for docs/no-inference-mode.md, not a submittable agent:
the screener requires the Rust root package contract. Run it next to a
practice-scope scorer (SCORED=0) and point DECISIONS_URL at a System One
endpoint.

No chat model is ever called. Every graded value is a span the host extracted
from delivered records, the user prompt or a tool result, and the TypeSafe Jev
decision model chooses among those candidates (choice) and decides
answerability / completion (noul). Retrieval is BM25 (no embeddings), so this
is a conservative floor for the mode.

Env:
  PORT                 listen port (default 7070)
  DECISIONS_URL        System One endpoint (default OpenRouter systemone)
  OPENROUTER_API_KEY   only for the direct OpenRouter path (local prototype)
  JEV_MODEL            default typesafe/jev-1.14
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DECISIONS_URL = os.environ.get(
    "DECISIONS_URL", "https://openrouter.ai/api/v1/systemone"
)
KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.14")

STATS = Counter()
STATS_LOCK = threading.Lock()
STORE: dict[str, list[dict]] = defaultdict(list)
STORE_LOCK = threading.Lock()
_CTX = threading.local()

WORD = re.compile(r"[A-Za-z0-9@._%$€£-]+")
STOP = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "or",
    "in",
    "on",
    "for",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "it",
    "this",
    "that",
    "with",
    "as",
    "at",
    "by",
    "from",
    "what",
    "who",
    "which",
    "when",
    "where",
    "how",
    "do",
    "does",
    "did",
    "my",
    "our",
    "we",
    "i",
    "you",
    "your",
    "me",
    "us",
    "their",
    "now",
    "about",
    "any",
    "there",
    "has",
    "have",
    "had",
    "will",
    "would",
    "should",
    "can",
    "could",
}


def toks(text: str) -> list[str]:
    return [t.lower().strip(".,") for t in WORD.findall(text) if t.lower() not in STOP]


class BM25:
    def __init__(self, docs: list[str]):
        self.docs = [toks(d) for d in docs]
        self.n = len(self.docs)
        self.avg = sum(len(d) for d in self.docs) / max(1, self.n)
        df = Counter()
        for d in self.docs:
            df.update(set(d))
        self.idf = {
            t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()
        }
        self.tf = [Counter(d) for d in self.docs]

    def top(self, query: str, k: int) -> list[int]:
        q = toks(query)
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            dl = len(self.docs[i])
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += (
                        self.idf.get(t, 0)
                        * f
                        * 2.2
                        / (f + 1.2 * (0.25 + 0.75 * dl / max(1, self.avg)))
                    )
            scores.append((s, i))
        scores.sort(reverse=True)
        return [i for s, i in scores[:k] if s > 0]


def jev(state, questions: dict) -> dict:
    body = {"model": MODEL, "state": state, "questions": questions}
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    for attempt in range(4):
        req = urllib.request.Request(DECISIONS_URL, data=data, headers=headers)
        try:
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            usage = out.get("usage") or {}
            with STATS_LOCK:
                STATS["decision_calls"] += 1
                STATS["decision_input_tokens"] += int(usage.get("input_tokens") or 0)
                STATS["decision_output_tokens"] += int(usage.get("output_tokens") or 0)
                STATS["decision_ms"] += int((time.time() - t0) * 1000)
                STATS["decision_questions"] += len(questions)
            return out.get("answers") or {}
        except urllib.error.HTTPError as e:
            with STATS_LOCK:
                STATS[f"decision_http_{e.code}"] += 1
            if e.code in (429, 502, 503, 504):
                time.sleep(1 + attempt)
                continue
            return {}
        except Exception:
            with STATS_LOCK:
                STATS["decision_errors"] += 1
            time.sleep(1 + attempt)
    return {}


# ---------------------------------------------------------------- candidates

NAME = re.compile(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,2})\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
NUM = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?%?(?![\w])")
DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?)\b"
)
TIME = re.compile(r"\b\d{1,2}(?::\d{2})? ?(?:am|pm|AM|PM)\b|\b\d{1,2}:\d{2}\b")
QUOTED = re.compile(r"[\"“']([^\"”']{2,60})[\"”']")
CUE = re.compile(
    r"\b(?:is now|is|are|was|to|as|by|via|over|named|called|owner is|moved to|switched to|set to|changed to|now)\s+"
    r"([A-Za-z0-9][\w@.\-']*(?: [\w@.\-']+){0,3})",
)
IDENT = re.compile(r"\b[a-z]+-[0-9a-f]{8,}(?:@[\w.]+)?\b|\bcontact-[0-9a-f]+@[\w.]+\b")


def candidates_from(texts: list[str], limit: int = 30) -> list[str]:
    seen: dict[str, int] = {}
    order = 0
    for text in texts:
        for rx in (EMAIL, IDENT, DATE, TIME, NUM, QUOTED, NAME, CUE):
            for m in rx.finditer(text):
                v = (m.group(1) if rx.groups else m.group(0)).strip(" .,;:")
                if not v or len(v) > 60 or v.lower() in STOP:
                    continue
                key = v.lower()
                if key not in seen:
                    seen[key] = order
                    order += 1
        if len(seen) > limit * 3:
            break
    return sorted(seen, key=lambda k: seen[k])[:limit]


def restore_case(cands: list[str], texts: list[str]) -> list[str]:
    joined = "\n".join(texts)
    out = []
    for c in cands:
        m = re.search(re.escape(c), joined, re.IGNORECASE)
        out.append(m.group(0) if m else c)
    return out


# ---------------------------------------------------------------- memory


def clauses(text: str) -> list[str]:
    return [
        c.strip()
        for c in re.split(r"[;\n]|(?<=[.!?])\s+|\s[\u2014\u2013-]\s|:\s", text)
        if len(c.strip()) > 2
    ]


def spans(clause: str, maxn: int = 5) -> list[str]:
    words = clause.split()
    out = []
    for n in range(1, maxn + 1):
        for i in range(len(words) - n + 1):
            sp = " ".join(words[i : i + n]).strip(" .,;:()\"'")
            if sp and sp.lower() not in STOP and sp not in out:
                out.append(sp)
    return out


ABSTAIN = "I don't have that in my records."


def handle_memory(req: dict) -> dict:
    user = req.get("user_id") or "miner"
    question = req.get("user_input") or ""
    with STORE_LOCK:
        pairs = list(STORE.get(user, []))
    if not pairs:
        return {"final_text": ABSTAIN, "abstain": True}
    docs = [p["prompt"] + " " + p["response"] for p in pairs]
    idx = BM25(docs).top(question, 8)
    picked = sorted((pairs[i] for i in idx), key=lambda p: p.get("timestamp", ""))
    records = [f"[{p.get('timestamp', '')[:10]}] {p['prompt']}" for p in picked]
    cls = []
    for p in picked:
        for c in clauses(p["prompt"]):
            if c not in cls:
                cls.append(c)
    cls = cls[-60:]
    if not cls:
        return {"final_text": ABSTAIN, "abstain": True}
    stage1 = jev(
        {"question": question, "records": records},
        {
            "answerable": {
                "type": "noul",
                "instructions": "Do these records state the information the question asks for? "
                "Answer no if the question's premise is wrong or the records never mention it.",
            },
            "clause": {
                "type": "choice",
                "instructions": "Which record passage states the answer to the question? Later records and corrections override earlier ones.",
                "criteria": {f"p{i}": c for i, c in enumerate(cls)},
            },
        },
    )
    noul = (stage1.get("answerable") or {}).get("noul")
    if noul is not None and noul < 0.2:
        return {"final_text": ABSTAIN, "abstain": True}
    key = (stage1.get("clause") or {}).get("choice") or ""
    if not key.startswith("p") or not key[1:].isdigit() or int(key[1:]) >= len(cls):
        return {"final_text": ABSTAIN, "abstain": True}
    clause = cls[int(key[1:])]
    qwords = set(toks(question))
    opts = [sp for sp in spans(clause) if not set(toks(sp)) <= qwords][:80]
    if not opts:
        return {"final_text": clause, "answer": clause}
    stage2 = jev(
        {"question": question, "passage": clause},
        {
            "answer": {
                "type": "choice",
                "instructions": "Which exact words from the passage answer the question?",
                "criteria": dict.fromkeys(opts),
            }
        },
    )
    choice = (stage2.get("answer") or {}).get("choice") or clause
    return {"final_text": choice, "answer": choice}


# ---------------------------------------------------------------- tools


def tool_text(t: dict) -> str:
    return t["name"].replace("_", " ") + " " + (t.get("description") or "")


def call_tool(
    endpoint: str, case_id: str, user_id: str, name: str, args: dict, hop: int
) -> str:
    body = json.dumps(
        {"case_id": case_id, "user_id": user_id, "name": name, "args": args, "hop": hop}
    ).encode()
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
        return str(out.get("result") or out.get("error") or "")
    except Exception as e:  # noqa: BLE001
        return f"tool error: {e}"


def fill_args(tool: dict, prompt: str, context: list[str]) -> dict:
    params = (tool.get("parameters") or {}).get("properties") or {}
    required = (tool.get("parameters") or {}).get("required") or []
    cands = restore_case(candidates_from([prompt] + context), [prompt] + context)
    args, questions = {}, {}
    for name in required:
        spec = params.get(name) or {}
        kind = spec.get("type")
        if spec.get("enum"):
            options = [str(v) for v in spec["enum"]]
        elif kind in ("integer", "number"):
            options = [c for c in cands if re.fullmatch(r"[\d.,]+", c)] or ["1"]
        elif kind == "boolean":
            options = ["true", "false"]
        elif "pair" in name.lower():
            with STORE_LOCK:
                pairs = [
                    p
                    for p in STORE.get(getattr(_CTX, "user", "") or "miner", [])
                    if not str(p.get("pair_id", "")).startswith("run-")
                ]
            top = (
                BM25([p["prompt"] + " " + p["response"] for p in pairs]).top(prompt, 8)
                if pairs
                else []
            )
            crit = {pairs[i]["pair_id"]: (pairs[i]["prompt"][:200]) for i in top}
            if crit:
                questions[name] = {
                    "type": "choice",
                    "instructions": f"Which saved memory does the request refer to (for '{name}')?",
                    "criteria": crit,
                }
                continue
            options = [prompt[:200]]
        else:
            options = cands[:20] + [prompt[:200]]
        if len(options) == 1:
            args[name] = options[0]
            continue
        questions[name] = {
            "type": "choice",
            "instructions": f"Value for the '{name}' argument of {tool['name']}: {spec.get('description', '')}"[
                :400
            ],
            "criteria": dict.fromkeys(dict.fromkeys(options)),
        }
    if questions:
        ans = jev(
            {"request": prompt, "tool": tool["name"], "context": context[-3:]},
            questions,
        )
        for name in questions:
            args[name] = (ans.get(name) or {}).get("choice") or next(
                iter(questions[name]["criteria"])
            )
            if (params.get(name) or {}).get("type") == "array":
                args[name] = [args[name]]
    for name, v in list(args.items()):
        kind = (params.get(name) or {}).get("type")
        if kind == "integer":
            with contextlib.suppress(ValueError):
                args[name] = int(float(str(v).replace(",", "")))
        elif kind == "number":
            with contextlib.suppress(ValueError):
                args[name] = float(str(v).replace(",", ""))
        elif kind == "boolean":
            args[name] = str(v).lower() == "true"
    return args


def handle_tools(req: dict) -> dict:
    prompt = req.get("user_input") or ""
    tools = req.get("tools") or []
    endpoint = req.get("tool_endpoint")
    case_id, user = req.get("case_id"), req.get("user_id") or ""
    _CTX.user = user
    usable = [
        t for t in tools if "connector to be linked" not in (t.get("description") or "")
    ]
    bm = BM25([tool_text(t) for t in usable])
    calls, results = [], []
    for hop in range(3):
        query = prompt + " " + " ".join(results[-1:])
        shortlist = [usable[i] for i in bm.top(query, 10)] or usable[:10]
        criteria = {t["name"]: (t.get("description") or "")[:240] for t in shortlist}
        criteria["no_tool"] = (
            "No further tool call is needed: the request is done, or no tool fits."
        )
        state = {
            "request": prompt,
            "calls_so_far": [
                {"tool": c["name"], "args": c["args"], "result": r[:400]}
                for c, r in zip(calls, results, strict=False)
            ],
        }
        questions = {
            "next": {
                "type": "choice",
                "instructions": "Which tool should run next to carry out the user's request?",
                "criteria": criteria,
            }
        }
        if hop == 0:
            questions["route"] = {
                "type": "choice",
                "instructions": "Classify the user's message. Questions about the user's own life, work, people, "
                "plans, records, preferences or earlier conversations are recall, even when they mention a tool's topic.",
                "criteria": {
                    "recall": "A question answered from what the user told me before: their records, notes, contacts, "
                    "plans, preferences, projects or past events.",
                    "act": "A request to do something (send, create, schedule, delete, change a setting, run) or to "
                    "fetch fresh outside information (web, news, prices, a link).",
                    "remember": "The user is telling me a fact or preference to keep; no question is asked.",
                    "chat": "A greeting or small talk.",
                },
            }
        ans = jev(state, questions)
        if hop == 0:
            route = (ans.get("route") or {}).get("choice")
            with STATS_LOCK:
                STATS[f"route_{route}"] += 1
            if route == "recall":
                return handle_memory(req)
            if route == "remember":
                cands = [c for c in candidates_from([prompt]) if len(c) > 1][:16]
                value = ""
                if cands:
                    a = jev(
                        {"message": prompt},
                        {
                            "value": {
                                "type": "choice",
                                "instructions": "Which value is the fact or preference the user wants kept?",
                                "criteria": dict.fromkeys(cands),
                            }
                        },
                    )
                    value = (a.get("value") or {}).get("choice") or ""
                return {
                    "final_text": f"Got it, noted: {value}."
                    if value
                    else "Got it, noted."
                }
            if route == "chat":
                return {
                    "final_text": "Doing well, thanks for asking! What can I help with?"
                }
        choice = (ans.get("next") or {}).get("choice")
        if not choice or choice == "no_tool":
            break
        tool = next(t for t in shortlist if t["name"] == choice)
        args = fill_args(tool, prompt, results)
        result = (
            call_tool(endpoint, case_id, user, tool["name"], args, hop)
            if endpoint
            else ""
        )
        calls.append({"name": tool["name"], "args": args, "hop": hop})
        results.append(result)
        if hop < 2:
            done = jev(
                {
                    "request": prompt,
                    "calls": [
                        {"tool": c["name"], "result": r[:400]}
                        for c, r in zip(calls, results, strict=False)
                    ],
                },
                {
                    "done": {
                        "type": "noul",
                        "instructions": "Is the user's request fully carried out by these tool calls?",
                    }
                },
            )
            if ((done.get("done") or {}).get("noul") or 0) >= 0.5:
                break
    final = results[-1][:600] if results else "Done."
    return {"final_text": final, "tool_calls": calls}


# ---------------------------------------------------------------- server


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/stats":
            with STATS_LOCK:
                return self._send(200, dict(STATS))
        self._send(404, {})

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
        )
        if self.path == "/seed":
            user = body.get("user_id") or "miner"
            pairs = body.get("pairs") or []
            with STORE_LOCK:
                have = {p["pair_id"] for p in STORE[user]}
                for p in pairs:
                    if p.get("pair_id") not in have:
                        STORE[user].append(p)
            return self._send(
                200,
                {
                    "pairs": len(pairs),
                    "subjects": len(body.get("subjects") or []),
                    "links": len(body.get("links") or []),
                },
            )
        if self.path == "/run":
            t0 = time.time()
            try:
                kind = "routed" if body.get("tool_endpoint") else "memory"
                with STATS_LOCK:
                    STATS[f"runs_{kind}"] += 1
                out = handle_tools(body) if kind == "routed" else handle_memory(body)
            except Exception as e:  # noqa: BLE001
                with STATS_LOCK:
                    STATS["run_errors"] += 1
                out = {"final_text": f"error: {e}"}
            out.setdefault("tool_calls", [])
            user = body.get("user_id") or "miner"
            with STORE_LOCK:
                STORE[user].append(
                    {
                        "pair_id": "run-" + str(body.get("case_id")),
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "prompt": body.get("user_input") or "",
                        "response": out.get("final_text") or "",
                    }
                )
            with open(os.environ.get("RUN_LOG", "/dev/null"), "a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "case_id": body.get("case_id"),
                            "q": body.get("user_input"),
                            "out": out,
                        }
                    )
                    + "\n"
                )
            out["prompt_tokens"] = 0
            out["output_tokens"] = 0
            out["latency_ms"] = int((time.time() - t0) * 1000)
            with STATS_LOCK:
                STATS["runs"] += 1
            return self._send(200, out)
        self._send(404, {})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7070"))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
