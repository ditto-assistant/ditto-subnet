# ruff: noqa: E501  (measurement prototype: long prompt strings)
"""Extractive-answer ceiling per v15/v16 memory family.

For each memory case: are the graded value(s) present verbatim (normalized)
in the text of its evidence pairs? If yes, a no-chat harness can in principle
surface them as candidate spans and let Jev choose. If no, the answer must be
computed/composed (arithmetic, unit conversion, abstention phrasing).
"""

import collections
import json
import re
import sys
import unicodedata


def norm(s):
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)
    s = re.sub(r"[^\w@.\-: ]+", " ", s)
    return " " + re.sub(r"\s+", " ", s).strip() + " "


def present(value, hay):
    v = norm(value).strip()
    if not v:
        return False
    return (" " + v + " ") in hay or (
        re.fullmatch(r"[\w@.\-]+", v) is not None and v in hay
    )


def items_for(c):
    if c.get("answer_items"):
        alts = c.get("answer_item_accept_any") or []
        out = []
        for i, it in enumerate(c["answer_items"]):
            a = [it] + (alts[i] if i < len(alts) and alts[i] else [])
            out.append(a)
        return out
    return [[c["expected_answer"]] + (c.get("accept_any") or [])]


def analyze(path):
    with open(path) as fh:
        d = json.load(fh)
    pairs = {}
    for w in d["memory_waves"]:
        for p in w.get("pairs") or []:
            pairs[p["pair_id"]] = p
    for c in d["tool_cases"]:
        for p in c.get("prerequisite_pairs") or []:
            pairs[p["pair_id"]] = p
    allhay = norm(" ".join(p["prompt"] + " " + p["response"] for p in pairs.values()))
    fam = collections.defaultdict(
        lambda: [0, 0, 0, 0]
    )  # n, evidence-extractive, anywhere-extractive, distractors-present
    kinds = collections.Counter()
    for c in d["memory_cases"]:
        f = c["question_type"]
        ev = [pairs[i] for i in c.get("v10_evidence_pair_ids") or [] if i in pairs]
        hay = norm(" ".join(p["prompt"] + " " + p["response"] for p in ev))
        its = items_for(c)
        kind = c.get("answer_kind")
        if kind in ("absence", "chitchat"):
            ok_ev = ok_any = None
        else:
            ok_ev = all(any(present(a, hay) for a in alts) for alts in its)
            ok_any = all(any(present(a, allhay) for a in alts) for alts in its)
        r = fam[f]
        r[0] += 1
        r[1] += 1 if ok_ev else 0
        r[2] += 1 if ok_any else 0
        r[3] += 1 if c.get("distractor_answers") else 0
        kinds[(kind, bool(ok_ev) if ok_ev is not None else "n/a")] += 1
    return fam, kinds, len(d["memory_cases"]), len(d["tool_cases"])


if __name__ == "__main__":
    total = collections.defaultdict(lambda: [0, 0, 0, 0])
    kinds_t = collections.Counter()
    for p in sys.argv[1:]:
        fam, kinds, nm, nt = analyze(p)
        print(f"# {p}: memory={nm} tool={nt}")
        for f, r in fam.items():
            t = total[f]
            for i in range(4):
                t[i] += r[i]
        kinds_t.update(kinds)
    n = sum(r[0] for r in total.values())
    e = sum(r[1] for r in total.values())
    print(f"overall extractive-in-evidence: {e}/{n} = {e / n:.2%}")
    for f, r in sorted(total.items(), key=lambda kv: -kv[1][0]):
        print(
            f"{f:45s} n={r[0]:4d} ev={r[1] / r[0]:6.1%} any={r[2] / r[0]:6.1%} distractors={r[3] / r[0]:5.0%}"
        )
    print("by answer_kind (kind, extractive):")
    for k, v in sorted(kinds_t.items(), key=lambda kv: str(kv[0])):
        print(" ", k, v)
