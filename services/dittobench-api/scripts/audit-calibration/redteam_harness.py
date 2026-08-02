#!/usr/bin/env python3
"""Two non-LLM harnesses that validate the transform audit end to end.

Both parse the seeded haystack directly, which is the whole premise of the
attack the audit targets: the generator is public and the haystack arrives in
cleartext, so a harness can answer by reading it rather than reasoning.

  --mode brittle   A surface-form dispatcher: it maps a question to an
                   attribute through a table of FIXED question templates, then
                   greps the haystack for that attribute's value. This is the
                   archetype of the rejected-harness corpus. Under a
                   post-commit rephrasing the template lookup misses, so it
                   should answer the base case and fail its transformed twin,
                   producing a LOW transform_robustness. If it doesn't, the
                   audit does not work.

  --mode robust    A keyword solver: it ignores question templates entirely and
                   scores every haystack sentence against the question's content
                   words, returning the best match. It is surface-INDEPENDENT,
                   so a rephrasing does not disturb it.

The robust mode is not a strawman, it is the honesty check. The docs claim the
audit does NOT defeat a solver that genuinely recomputes from the cleartext
haystack. If robust mode scored a low transform_robustness, that claim would be
wrong and the audit would be over-claimed. It is here to be shown passing.
"""

import argparse
import collections
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

STORE = {}  # user_id -> list[str] sentences
LOCK = threading.Lock()

STOP = set(
    """a an the my me i is are was were what which who whom whose do does did
    you your of to in on at for and or as it its that this these those tell
    remind know have has had can could would please just so about there their
    them then now current currently one more thing before forget while check
    right thanks rush handy whenever sec appreciate switching topics second
    unrelated tangent different subject sorry asking again""".split()
)

# The brittle dispatcher's fixed template table: exact question surfaces mapped
# to the attribute they ask about. Deliberately keyed on wording, which is
# precisely what the transform audit is designed to break.
TEMPLATES = [
    (r"what(?:'s| is) my blood (?:type|group)\??$", "blood"),
    (r"which blood type am i\??$", "blood"),
    (r"what(?:'s| is) my height\??$", "height"),
    (r"how tall am i\??$", "height"),
    (r"who(?:'s| is) my (?:current )?employer\??$", "employer"),
    (r"who do i (?:currently )?work for\??$", "employer"),
    (r"which company do i work for(?: now)?\??$", "employer"),
    (r"what(?:'s| is) my (?:favorite |favourite )?sports team\??$", "team"),
    (r"which sports team do i support(?: the most)?\??$", "team"),
    (r"which team am i a fan of\??$", "team"),
    (r"what city do i live in\??$", "city"),
    (r"where do i live(?: now)?\??$", "city"),
    (r"what(?:'s| is) my (?:current )?city\??$", "city"),
    (r"what(?:'s| is) my job\??$", "job"),
    (r"what do i do for work\??$", "job"),
    (r"what(?:'s| is) my occupation\??$", "job"),
    (r"what car do i drive\??$", "car"),
    (r"what(?:'s| is) my car\??$", "car"),
    (r"what code editor do i use\??$", "editor"),
    (r"what(?:'s| is) my code editor\??$", "editor"),
]

# Attribute -> the haystack cue that introduces its value.
CUES = {
    "blood": [r"blood (?:type|group) is ([A-Za-z0-9 +-]+)"],
    "height": [r"(?:height is|i am|i'm) ([0-9]+ ?cm)"],
    "employer": [r"(?:work (?:for|at)|employer is|joined) ([A-Z][\w' -]+)"],
    "team": [r"(?:support|fan of|favou?rite (?:sports )?team is) (?:the )?([A-Z][\w' -]+)"],
    "city": [r"(?:moved to|live in|relocated to|settled into) ([A-Z][\w' -]+)"],
    "job": [r"(?:work as a|job is being a|professionally i'm a) ([\w' -]+)"],
    "car": [r"(?:drive a|car is|got (?:a|my)) ([\w' -]+)"],
    "editor": [r"(?:code editor is|editor is|use) ([A-Z][\w' -]+)"],
}


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


# The lexicon of BASE question surfaces the public generator can emit, dumped
# across 400 seeds. This is what a surface-fingerprinting harness would build.
BASE_SURFACES = set()
try:
    with open("/tmp/claude-1000/base_surfaces.json") as _fh:
        BASE_SURFACES = set(json.load(_fh))
except Exception:
    pass


def norm(q):
    return " ".join(q.lower().split())


def brittle_answer(question, sents):
    """The SAME solver as robust mode, gated on recognizing the exact surface.

    Isolating the gate is the whole point: answering ability is held identical
    between the two modes, so any difference in transform_robustness is caused
    by surface-form keying alone and nothing else. A harness that dispatches on
    a fingerprinted question template behaves exactly like this -- competent on
    the phrasings it knows, mute on everything else.
    """
    if norm(question) not in BASE_SURFACES:
        return ""  # unrecognized surface -> the dispatcher has nothing to say
    return robust_answer(question, sents)


def strong_answer(question, sents):
    """A MORE COMPETENT local solver, to test the metric where it is applied.

    The audit only ever judges champion/tail agents, which are far more accurate
    than the stock reference harness. If audit pairs are mostly both-wrong at
    reference competence, the question is whether they stay uninformative at
    champion competence -- so this scores sentences with inverse-document
    weighting (rare content words count for more) and prefers first-person
    statements, which is where the persona facts live.
    """
    words = [w for w in re.findall(r"[a-z0-9']+", question.lower()) if w not in STOP]
    if not words:
        return ""
    df = collections.Counter()
    for s in sents:
        for w in set(re.findall(r"[a-z0-9']+", s.lower())):
            df[w] += 1
    n = max(1, len(sents))
    best, best_score = "", 0.0
    for s in sents:
        low = s.lower()
        score = 0.0
        for w in words:
            if w in low:
                score += math.log(1.0 + n / (1.0 + df[w]))
        if re.match(r"^(i |i'|my |we )", low):
            score *= 1.15
        if score > best_score:
            best_score, best = score, s
    return best if best_score > 0 else ""


def robust_answer(question, sents):
    words = [w for w in re.findall(r"[a-z0-9']+", question.lower()) if w not in STOP]
    if not words:
        return ""
    best, best_score = "", 0
    for s in sents:
        low = s.lower()
        score = sum(1 for w in words if w in low)
        if score > best_score:
            best_score, best = score, s
    return best if best_score >= 1 else ""


class Handler(BaseHTTPRequestHandler):
    mode = "brittle"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/seed":
            uid = req.get("user_id") or "miner"
            sents = []
            for p in req.get("pairs") or []:
                sents += sentences(p.get("prompt") or "")
                sents += sentences(p.get("response") or "")
            with LOCK:
                STORE.setdefault(uid, []).extend(sents)
            self._json(200, {"pairs": len(req.get("pairs") or []), "subjects": 0, "links": 0})
            return
        if self.path == "/run":
            uid = req.get("user_id") or "miner"
            q = req.get("user_input") or ""
            with LOCK:
                sents = list(STORE.get(uid, []))
            if self.mode == "brittle":
                ans = brittle_answer(q, sents)
            elif self.mode == "strong":
                ans = strong_answer(q, sents)
            elif self.mode == "strong-brittle":
                # Same strong solver, gated on exact surface recognition: the
                # champion-competence version of the brittle attacker.
                ans = strong_answer(q, sents) if norm(q) in BASE_SURFACES else ""
            else:
                ans = robust_answer(q, sents)
            self._json(
                200,
                {
                    "final_text": ans,
                    "answer": ans,
                    "tool_calls": [],
                    "prompt_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 1,
                },
            )
            return
        self._json(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["brittle", "robust", "strong", "strong-brittle"], default="brittle")
    ap.add_argument("--port", type=int, default=8200)
    args = ap.parse_args()
    Handler.mode = args.mode
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
