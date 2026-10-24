import json, collections, math, sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lme-study/full.jsonl"
rows = []
for l in open(PATH):
    try: rows.append(json.loads(l))
    except Exception: pass

# group: arm -> probe_id -> [rep records]
by = collections.defaultdict(lambda: collections.defaultdict(list))
for r in rows:
    by[r["model_arm"]][r["probe_id"]].append(r)

REPS = 3
# A row with no "label" is an API error, not a verdict. An arm that errored on
# every call would otherwise read as perfectly stable with 0% empty responses:
# a zero that could never have been non-zero.
complete = {a: {p: [x for x in v if "label" in x] for p, v in d.items()}
            for a, d in by.items()}
complete = {a: {p: v for p, v in d.items() if len(v) >= 2}
            for a, d in complete.items()}
unusable = {a: len(by[a]) for a, d in complete.items() if len(d) < 1800}
complete = {a: d for a, d in complete.items() if len(d) >= 1800}
if unusable:
    print("ARMS EXCLUDED (too few labelled verdicts):")
    for a in sorted(unusable):
        errs = sum(1 for p in by[a] for x in by[a][p] if "error" in x)
        tot = sum(len(v) for v in by[a].values())
        print("  %-46s %d/%d calls errored (%.0f%%)" % (a, errs, tot, 100*errs/tot))
    print()

def modal(recs):
    labs = [x.get("label") for x in recs[:REPS] if "label" in x]
    if not labs: return None
    return sum(1 for x in labs if x) * 2 > len(labs)

def unstable(recs):
    labs = [x.get("label") for x in recs[:REPS] if "label" in x]
    return len(set(labs)) > 1 if len(labs) >= 2 else False

def empty_rate(recs):
    n = e = 0
    for x in recs[:REPS]:
        if "raw" in x:
            n += 1
            if not x["raw"].strip(): e += 1
    return e, n

print("ARMS COMPLETE (>=%d reps on >=1800 probes): %d\n" % (REPS, len(complete)))

# ---------- 1. protocol compatibility ----------
print("=" * 94)
print("1. PROTOCOL COMPATIBILITY  (official evaluator: max_tokens=10, label = 'yes' in text)")
print("=" * 94)
print("%-46s %10s %10s %12s" % ("arm", "empty%", "trunc%", "silent-NO%"))
for a in sorted(complete):
    recs = [x for p in complete[a] for x in complete[a][p][:REPS]]
    n = len(recs)
    emp = sum(1 for x in recs if "raw" in x and not x["raw"].strip())
    trunc = sum(1 for x in recs if x.get("finish") == "length")
    # silent NO: empty content that the extractor turns into a negative verdict
    silent = sum(1 for x in recs if "raw" in x and not x["raw"].strip() and x.get("label") is False)
    print("%-46s %9.1f%% %9.1f%% %11.1f%%" % (a, 100*emp/n, 100*trunc/n, 100*silent/n))

# ---------- 2. stability ----------
print()
print("=" * 94)
print("2. STABILITY ACROSS %d IDENTICAL REPLICATES (temperature=0)" % REPS)
print("=" * 94)
print("%-46s %12s %14s" % ("arm", "unstable", "flip-rate"))
for a in sorted(complete):
    d = complete[a]; u = sum(1 for p in d if unstable(d[p]))
    print("%-46s %9d/%-5d %12.2f%%" % (a, u, len(d), 100*u/len(d)))

# ---------- 3. gold conformance ----------
print()
print("=" * 94)
print("3. CLAUSE CONFORMANCE vs GOLD (gold derived from the official prompt templates)")
print("=" * 94)
fams = sorted({r["probe"] for r in rows})
short = {f: (f.split("_")[0] + ("t" if f.endswith("temporal") and "non" not in f else ("n" if f.endswith("nontemporal") else ""))) for f in fams}
print("%-46s %7s  %s" % ("arm", "ALL", "".join("%-7s" % short[f] for f in fams)))
probe_of = {r["probe_id"]: r["probe"] for r in rows}
qtype_of = {r["probe_id"]: r["question_type"] for r in rows}
def scorable(pid):
    return not (probe_of[pid] in ("P1_exact", "P2_verbose")
                and qtype_of[pid] == "single-session-preference")
def _gold(a):
    sc = [p for p in complete[a] if scorable(p)]
    return sum(1 for p in sc if modal(complete[a][p]) == complete[a][p][0]["gold"]) / max(len(sc), 1)
for a in sorted(complete, key=lambda a: -_gold(a)):
    d = complete[a]
    sc = [p for p in d if scorable(p)]
    tot = sum(1 for p in sc if modal(d[p]) == d[p][0]["gold"])
    line = "%-46s %6.1f%%  " % (a, 100*tot/len(sc))
    for f in fams:
        ps = [p for p in d if probe_of[p] == f and scorable(p)]
        if ps:
            c = sum(1 for p in ps if modal(d[p]) == d[p][0]["gold"])
            line += "%-7s" % ("%.0f%%" % (100*c/len(ps)))
        else:
            line += "%-7s" % "-"
    print(line)
print("\nkey: " + "  ".join("%s=%s" % (short[f], f) for f in fams))


# ---------- 4. agreement with the incumbent ----------
REF = "openai/gpt-4o-2024-08-06@official"
if REF in complete:
    print()
    print("=" * 94)
    print("4. AGREEMENT WITH THE INCUMBENT (%s), modal verdict over %d reps" % (REF, REPS))
    print("=" * 94)
    ref = {p: modal(v) for p, v in complete[REF].items()}
    print("%-46s %10s %9s %10s %10s" % ("arm", "agree%", "kappa", "yes-bias", "n"))
    for a in sorted(complete, key=lambda a: -sum(1 for p in complete[a] if p in ref and modal(complete[a][p]) == ref[p]) / max(len(complete[a]), 1)):
        if a == REF: continue
        shared = [p for p in complete[a] if p in ref]
        if len(shared) < 1000: continue
        n = len(shared)
        agree = sum(1 for p in shared if modal(complete[a][p]) == ref[p])
        a_yes = sum(1 for p in shared if modal(complete[a][p]))
        r_yes = sum(1 for p in shared if ref[p])
        po = agree / n
        pe = (a_yes/n)*(r_yes/n) + (1-a_yes/n)*(1-r_yes/n)
        kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
        print("%-46s %9.1f%% %9.3f %9.1f%% %10d" % (a, 100*po, kappa, 100*(a_yes-r_yes)/n, n))
    print()
    print("  yes-bias = (candidate YES rate - incumbent YES rate); positive = more lenient")

# ---------- 5. score translation ----------
# If a candidate judge J has, relative to the incumbent,
#   alpha = P(J says yes | 4o says yes)      (sensitivity)
#   beta  = P(J says no  | 4o says no)       (specificity)
# then an agent whose true-under-4o accuracy is S scores, under J,
#   S_J = alpha*S + (1 - beta)*(1 - S)
# and the inverse (Rogan-Gladen) recovers the 4o-denominated score:
#   S_4o = (S_J - (1 - beta)) / (alpha - (1 - beta))
# The denominator is Youden's J = alpha + beta - 1. As it approaches 0 the
# translation becomes unstable, which is the honest way to say "this judge
# cannot be converted back".
if REF in complete:
    print()
    print("=" * 94)
    print("5. SCORE TRANSLATION  (alpha/beta estimated per question type vs the incumbent)")
    print("=" * 94)
    ref = {p: modal(v) for p, v in complete[REF].items()}
    TYPE_N = {"knowledge-update": 78, "multi-session": 133,
              "single-session-assistant": 56, "single-session-preference": 30,
              "single-session-user": 70, "temporal-reasoning": 133}
    print("%-46s %8s %8s %9s %12s" % ("arm", "alpha", "beta", "youden", "translatable"))
    trans = {}
    for a in sorted(complete):
        if a == REF: continue
        shared = [p for p in complete[a] if p in ref]
        if len(shared) < 1000: continue
        tp = sum(1 for p in shared if ref[p] and modal(complete[a][p]))
        pos = sum(1 for p in shared if ref[p])
        tn = sum(1 for p in shared if not ref[p] and not modal(complete[a][p]))
        neg = sum(1 for p in shared if not ref[p])
        if pos == 0 or neg == 0: continue
        al, be = tp/pos, tn/neg
        j = al + be - 1
        trans[a] = (al, be, j)
        print("%-46s %8.3f %8.3f %9.3f %12s" % (
            a, al, be, j, "yes" if j > 0.5 else ("marginal" if j > 0.2 else "NO")))
    print()
    print("  S_J = alpha*S + (1-beta)*(1-S)     S_4o = (S_J - (1-beta)) / youden")
    print("  youden <= 0.2 means the judge destroys too much signal to convert back.")

    # project the frozen 2026-07-23 leaderboard under each candidate
    import os as _os
    lb = _os.environ.get("LME_LEADERBOARD", "")
    if lb and _os.path.exists(lb):
        board = json.load(open(lb))
        print()
        print("  PROJECTED 2026-07-23 LEADERBOARD UNDER EACH JUDGE")
        print("  (projection, not a measurement: alpha/beta come from the probe suite,")
        print("   whose hypothesis mixture is adversarial, not agent-realistic)")
        pertype = collections.defaultdict(dict)
        for a in trans:
            shared = [p for p in complete[a] if p in ref]
            for qt in TYPE_N:
                s = [p for p in shared if qtype_of[p] == qt]
                pos = [p for p in s if ref[p]]; neg = [p for p in s if not ref[p]]
                if not pos or not neg: continue
                al = sum(1 for p in pos if modal(complete[a][p]))/len(pos)
                be = sum(1 for p in neg if not modal(complete[a][p]))/len(neg)
                pertype[a][qt] = (al, be)
        names = [ag["name"] for ag in board["agents"]]
        print("  %-44s %s" % ("judge", "".join("%-16s" % n[:15] for n in names)))
        print("  %-44s %s" % ("gpt-4o (actual)",
              "".join("%-16s" % ("%.1f%%" % (100*ag["accuracy"])) for ag in board["agents"])))
        for a in sorted(trans, key=lambda x: -trans[x][2]):
            cells = []
            for ag in board["agents"]:
                num = den = 0.0
                for qt, meta in ag["per_category"].items():
                    if qt not in pertype[a]: continue
                    al, be = pertype[a][qt]; S = meta["accuracy"]; n = meta["n"]
                    num += n * (al*S + (1-be)*(1-S)); den += n
                cells.append("%-16s" % ("%.1f%%" % (100*num/den) if den else "n/a"))
            print("  %-44s %s" % (a[:43], "".join(cells)))
