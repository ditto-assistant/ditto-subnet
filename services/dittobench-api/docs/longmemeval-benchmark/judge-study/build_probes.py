"""Deterministic clause-conformance probes for the official LongMemEval judge.

Gold labels are derived from the five official prompt templates in
LongMemEval's evaluate_qa.py (rev 9e0b455f), not from any model.
Reproducible from the public oracle dataset + SEED alone.
"""
import json, re, sys, hashlib, collections

SEED = 20261023
INT = re.compile(r"(?<![\d.,])(\d{1,4})(?![\d.,])")
DUR = re.compile(r"^\s*(\d{1,4})\s*(day|days|week|weeks|month|months)\b", re.I)
BARE = re.compile(r"^\s*(\d{1,4})\s*\.?\s*$")
HEDGE = re.compile(r"acceptable|approximately|\bor\b|~|either", re.I)

def hash_idx(key, n):
    return int(hashlib.sha256(("%d:%s" % (SEED, key)).encode()).hexdigest(), 16) % n

def pick_alt(qid, ans, alts, salt=""):
    """Deterministically pick an alternative answer that cannot be read as correct."""
    cands = [a for a in alts
             if a != ans
             and a.lower() not in ans.lower()
             and ans.lower() not in a.lower()]
    return cands[hash_idx(qid + salt, len(cands))] if cands else None

def clean_subset(ans):
    """Split only on explicit conjunctions joining two substantive facts."""
    if re.match(r"^\s*(yes|no)\b", ans, re.I):
        return None
    for sep in [";", " and "]:
        if sep in ans:
            parts = [p.strip(" .,") for p in ans.split(sep) if p.strip(" .,")]
            if len(parts) >= 2 and all(len(p.split()) >= 2 for p in parts):
                return parts
    return None

def offbyone(ans, temporal):
    """Return (hypothesis, n, shifted) or None. Never lands on a value the
    reference text already names as acceptable."""
    if temporal:
        m = DUR.match(ans)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            for cand in ([n - 1, n + 1] if n >= 2 else [n + 1]):
                if not re.search(r"(?<![\d.,])%d(?![\d.,])" % cand, ans):
                    return ("%d %s" % (cand, unit), n, cand)
            return None
        m = BARE.match(ans)
        if m:
            n = int(m.group(1))
            for cand in ([n - 1, n + 1] if n >= 2 else [n + 1]):
                return (str(cand), n, cand)
        return None
    # non-temporal: require exactly one number and no hedge naming alternatives
    if HEDGE.search(ans):
        return None
    nums = INT.findall(ans)
    if len(nums) != 1:
        return None
    n = int(nums[0])
    cand = n + 1
    if re.search(r"(?<![\d.,])%d(?![\d.,])" % cand, ans):
        return None
    m = INT.search(ans)
    return (ans[:m.start(1)] + str(cand) + ans[m.end(1):], n, cand)

def probes_for(e, pool):
    qid, qt, ans = e["question_id"], e["question_type"], str(e["answer"]).strip()
    abst = "_abs" in qid
    out = []
    def add(pid, hyp, gold, clause):
        out.append({"probe_id": "%s::%s" % (qid, pid), "question_id": qid,
                    "question_type": qt, "abstention": abst, "probe": pid,
                    "hypothesis": hyp, "gold": gold, "clause": clause})

    add("P1_exact", ans, True, "answer yes if the response contains the correct answer")
    add("P2_verbose",
        "Based on what I can recall from our previous conversations, and I want to be "
        "careful here because several related details came up across different "
        "sessions, the most likely answer is: %s. Let me know if you would like me to "
        "double-check any part of that." % ans,
        True, "answer yes if the response contains the correct answer")

    alts = pool.get(qt, [])
    if not abst:
        alt = pick_alt(qid, ans, alts)
        if alt:
            add("P6_wrong", alt, False, "otherwise, answer no")
        if qt != "single-session-preference":
            parts = clean_subset(ans)
            if parts:
                add("P3_subset", parts[0], False,
                    "if the response only contains a subset of the information "
                    "required by the answer, answer no")
            ob = offbyone(ans, qt == "temporal-reasoning")
            if ob:
                hyp, n, cand = ob
                if qt == "temporal-reasoning":
                    add("P4_offbyone_temporal", hyp, True,
                        "do not penalize off-by-one errors for the number of days")
                else:
                    add("P4_offbyone_nontemporal", hyp, False,
                        "no off-by-one allowance outside temporal-reasoning")
        if qt == "knowledge-update":
            stale = pick_alt(qid, ans, alts, "s")
            if stale:
                add("P5_stale_then_updated",
                    "Earlier you mentioned %s, but you later updated that — the "
                    "current answer is %s." % (stale, ans),
                    True, "previous information along with an updated answer ... correct")
                add("P7_updated_to_wrong",
                    "Earlier you mentioned %s, but you later updated that — the "
                    "current answer is %s." % (ans, stale),
                    False, "as long as the updated answer is the required answer")
    else:
        add("P8_explicit_unanswerable",
            "I don't have enough information to answer that. Looking back through our "
            "conversations, that specific detail was never mentioned.",
            True, "correctly identifies the question as unanswerable")
        alt = pick_alt(qid, ans, alts, "f")
        if alt:
            add("P9_fabricated", alt, False,
                "model fails to identify the question as unanswerable")
    return out

def main():
    data = json.load(open(sys.argv[1]))
    pool = collections.defaultdict(list)
    for e in sorted(data, key=lambda x: x["question_id"]):
        if "_abs" not in e["question_id"]:
            pool[e["question_type"]].append(str(e["answer"]).strip())
    probes = []
    for e in sorted(data, key=lambda x: x["question_id"]):
        probes.extend(probes_for(e, pool))
    with open(sys.argv[2], "w") as f:
        for p in probes:
            f.write(json.dumps(p, sort_keys=True) + "\n")
    c = collections.Counter(p["probe"] for p in probes)
    gold = {p["probe"]: p["gold"] for p in probes}
    print("probes: %d" % len(probes))
    for k, v in sorted(c.items()):
        print("  %-28s n=%-5d gold=%s" % (k, v, "YES" if gold[k] else "NO"))
    print("sha256:", hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest())

main()
