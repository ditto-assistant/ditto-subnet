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
complete = {a: {p: v for p, v in d.items() if len(v) >= REPS}
            for a, d in by.items()}
complete = {a: d for a, d in complete.items() if len(d) >= 1800}

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
