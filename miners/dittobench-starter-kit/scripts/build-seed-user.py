#!/usr/bin/env python3
"""Build a committable seed-user fixture for the dittobench-starter-kit.

This script selects a coherent, variety-balanced slice of the LongMemEval
``dittobench_lme_fixture`` and writes a small set of JSON fixtures that the
starter kit can load directly. The kit re-embeds at load time, so embeddings
are dropped from every output.

MAINTAINERS ONLY: the inputs below are NOT distributed with the starter kit —
regenerating the fixture requires the private LongMemEval source data. Miners
never need to run this; the generated fixture ships under fixtures/seed-user/.

Inputs (READ-ONLY, not committed to the starter kit):

  Base A ($LME_BASE_A or argv[1]):
      seed_pairs.json         list of {pair_id, prompt, response, ...}
      seed_subjects.json      list of {id, subject_text, description_text,
                                       subject_type, embedding}  (embedding dropped)
      seed_subject_links.json list of {subject_id, firestore_pair_id}
      seed_manifest.json      {fixture_user, question_count, total_pairs,
                               cases:[{question_id, session_to_pairs, pair_count}]}

  Base B ($LME_BASE_B or argv[2]):
      longmemeval_oracle.json list of {question_id, question_type, question,
                                       answer, question_date, haystack_dates,
                                       haystack_session_ids, haystack_sessions,
                                       answer_session_ids}

Output ($SEED_OUT or argv[3], typically fixtures/seed-user in this repo):
    pairs.json          {pair_id, session_id, timestamp(RFC3339 Z), prompt, response}
    subjects.json       {id, subject_text, description_text}
    subject_links.json  {subject_id, pair_id}
    memory_cases.json   {question_id, question_type, query, answer, answer_pair_ids}
    manifest.json       {fixture_user, generated_from, question_count, pair_count,
                         subject_count, link_count, selected_question_ids}

Every path is required, via argv (positional: base_a base_b out_dir) or env
(LME_BASE_A / LME_BASE_B / SEED_OUT).

Usage:
    python3 build-seed-user.py <base_a> <base_b> <out_dir>
"""

import json
import os
import re
import sys
from collections import defaultdict, OrderedDict

MAX_QUESTIONS = 50
PAIRS_SIZE_CAP = 4 * 1024 * 1024  # ~4 MB

# "2023/04/10 (Mon) 17:50" -> RFC3339 "2023-04-10T17:50:00Z"
_DATE_RE = re.compile(
    r"^\s*(\d{4})/(\d{2})/(\d{2})\s*(?:\([^)]*\))?\s*(\d{2}):(\d{2})(?::(\d{2}))?\s*$"
)


def parse_haystack_date(s):
    """Parse a haystack date like '2023/04/10 (Mon) 17:50' to RFC3339 UTC.

    Returns None if the value cannot be parsed.
    """
    if not s:
        return None
    m = _DATE_RE.match(s)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    ss = ss or "00"
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}Z"


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def required_path(arg_idx, env_var, what):
    """A required input path, from positional argv or the named env var."""
    if len(sys.argv) > arg_idx:
        return sys.argv[arg_idx]
    value = os.environ.get(env_var)
    if not value:
        sys.exit(
            f"missing {what}: pass it as positional arg {arg_idx} or set ${env_var} "
            "(inputs are not distributed with the kit — maintainers only)"
        )
    return value


def main():
    base_a = required_path(1, "LME_BASE_A", "the LongMemEval seed fixture dir (base_a)")
    base_b = required_path(2, "LME_BASE_B", "the LongMemEval oracle dir (base_b)")
    out_dir = required_path(3, "SEED_OUT", "the output fixtures/seed-user dir")

    p = lambda base, name: os.path.join(base, name)

    print(f"base_a = {base_a}")
    print(f"base_b = {base_b}")
    print(f"out    = {out_dir}")

    # --- Load inputs -------------------------------------------------------
    seed_pairs = load_json(p(base_a, "seed_pairs.json"))
    seed_links = load_json(p(base_a, "seed_subject_links.json"))
    seed_manifest = load_json(p(base_a, "seed_manifest.json"))
    oracle = load_json(p(base_b, "longmemeval_oracle.json"))

    pairs_by_id = {r["pair_id"]: r for r in seed_pairs}
    oracle_by_qid = {r["question_id"]: r for r in oracle}
    manifest_by_qid = {c["question_id"]: c for c in seed_manifest["cases"]}

    # --- 1/2. Candidates present in BOTH, sorted --------------------------
    candidates = sorted(set(manifest_by_qid) & set(oracle_by_qid))
    print(f"candidates (in both manifest+oracle): {len(candidates)}")

    # --- 3. Round-robin select up to 50 across question_type --------------
    by_type = OrderedDict()
    for qid in candidates:  # already sorted -> groups preserve sorted order
        by_type.setdefault(oracle_by_qid[qid]["question_type"], []).append(qid)

    type_order = sorted(by_type)  # deterministic type ordering
    queues = {t: list(by_type[t]) for t in type_order}
    selected_qids = []
    while len(selected_qids) < MAX_QUESTIONS:
        progressed = False
        for t in type_order:
            if queues[t]:
                selected_qids.append(queues[t].pop(0))
                progressed = True
                if len(selected_qids) >= MAX_QUESTIONS:
                    break
        if not progressed:
            break
    print(f"selected after round-robin: {len(selected_qids)}")

    # --- Build per-question pair mappings ---------------------------------
    def build_for_qids(qids):
        """Return (pair_records, q_answer_pairs, unresolved_notes).

        pair_records: dict pair_id -> {pair_id, session_id, timestamp, prompt, response}
        q_answer_pairs: dict qid -> list of answer pair_ids (ordered, deduped)
        """
        pair_records = OrderedDict()
        q_all_pairs = {}
        q_answer_pairs = {}
        unresolved = []
        missing_count = 0
        missing_ids = set()

        for qid in qids:
            case = manifest_by_qid[qid]
            orc = oracle_by_qid[qid]
            hs_ids = orc.get("haystack_session_ids") or []
            hs_dates = orc.get("haystack_dates") or []
            s2p = case["session_to_pairs"]

            all_pairs = []
            for k in sorted(s2p, key=lambda x: int(x)):
                idx = int(k)
                session_id = hs_ids[idx] if idx < len(hs_ids) else f"{qid}_s{k}"
                raw_date = hs_dates[idx] if idx < len(hs_dates) else None
                ts = parse_haystack_date(raw_date)
                for pid in s2p[k]:
                    all_pairs.append(pid)
                    rec = pairs_by_id.get(pid)
                    if rec is None:
                        if pid not in missing_ids:
                            missing_ids.add(pid)
                        continue
                    if pid not in pair_records:
                        pair_records[pid] = {
                            "pair_id": pid,
                            "session_id": session_id,
                            "timestamp": ts,
                            "prompt": rec.get("prompt", ""),
                            "response": rec.get("response", ""),
                        }
            q_all_pairs[qid] = all_pairs

            # answer_pair_ids: map answer_session_ids -> index in haystack_session_ids
            ans_pairs = []
            resolved_any = False
            sid_to_idx = {sid: i for i, sid in enumerate(hs_ids)}
            for sid in (orc.get("answer_session_ids") or []):
                if sid in sid_to_idx:
                    k = str(sid_to_idx[sid])
                    if k in s2p:
                        resolved_any = True
                        ans_pairs.extend(s2p[k])
            # intersect with pairs that actually exist in pair_records (selected pairs)
            if resolved_any:
                seen = set()
                ans_pairs = [
                    pid for pid in ans_pairs
                    if pid in pair_records and not (pid in seen or seen.add(pid))
                ]
            else:
                # fallback: union of all the question's pairs (that exist)
                seen = set()
                ans_pairs = [
                    pid for pid in all_pairs
                    if pid in pair_records and not (pid in seen or seen.add(pid))
                ]
                unresolved.append(qid)
            q_answer_pairs[qid] = ans_pairs

        missing_count = len(missing_ids)
        return pair_records, q_answer_pairs, unresolved, missing_count

    # --- 4. Size cap: drop from END until pairs.json <= ~4 MB -------------
    while selected_qids:
        pair_records, q_answer_pairs, unresolved, missing_count = build_for_qids(selected_qids)
        pairs_list = list(pair_records.values())
        blob = json.dumps(pairs_list, indent=2)
        if len(blob.encode("utf-8")) <= PAIRS_SIZE_CAP:
            break
        dropped = selected_qids.pop()
        # loop and rebuild

    selected_pair_ids = set(pair_records.keys())
    print(f"final question count: {len(selected_qids)}")
    print(f"missing pair_ids (in manifest but not seed_pairs): {missing_count}")
    if unresolved:
        print(f"answer_session mapping unresolved (fell back to all pairs) for "
              f"{len(unresolved)} questions: {unresolved}")

    # --- 2. subjects: linked to >=1 selected pair -------------------------
    # links use field name 'firestore_pair_id'
    links_for_selected = [
        l for l in seed_links if l.get("firestore_pair_id") in selected_pair_ids
    ]
    selected_subject_ids = {l["subject_id"] for l in links_for_selected}

    # Load subjects (large; drop embedding). Stream object-by-object so we
    # never hold 81 MB of embedding strings in memory at once is overkill —
    # json.load is fine per the task. We just drop the embedding key.
    seed_subjects = load_json(p(base_a, "seed_subjects.json"))
    subjects_out = []
    for s in seed_subjects:
        if s["id"] in selected_subject_ids:
            subjects_out.append({
                "id": s["id"],
                "subject_text": s.get("subject_text", ""),
                "description_text": s.get("description_text", ""),
            })
    del seed_subjects  # free memory
    present_subject_ids = {s["id"] for s in subjects_out}

    # --- 3. subject_links filtered to selected pairs AND selected subjects -
    subject_links_out = [
        {"subject_id": l["subject_id"], "pair_id": l["firestore_pair_id"]}
        for l in links_for_selected
        if l["subject_id"] in present_subject_ids
    ]

    # --- 4. memory_cases ---------------------------------------------------
    memory_cases = []
    for qid in selected_qids:
        orc = oracle_by_qid[qid]
        memory_cases.append({
            "question_id": qid,
            "question_type": orc["question_type"],
            "query": orc["question"],
            "answer": orc["answer"],
            "answer_pair_ids": q_answer_pairs[qid],
        })

    pairs_list = list(pair_records.values())

    # --- 5. manifest -------------------------------------------------------
    out_manifest = {
        "fixture_user": seed_manifest.get("fixture_user"),
        "generated_from": "longmemeval dittobench_lme_fixture",
        "question_count": len(selected_qids),
        "pair_count": len(pairs_list),
        "subject_count": len(subjects_out),
        "link_count": len(subject_links_out),
        "selected_question_ids": selected_qids,
    }

    # --- Write -------------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)

    def write(name, obj):
        path = os.path.join(out_dir, name)
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
        return path, os.path.getsize(path)

    outputs = [
        write("pairs.json", pairs_list),
        write("subjects.json", subjects_out),
        write("subject_links.json", subject_links_out),
        write("memory_cases.json", memory_cases),
        write("manifest.json", out_manifest),
    ]

    # --- Verify / report ---------------------------------------------------
    print("\n=== OUTPUT FILES ===")
    for path, size in outputs:
        print(f"  {os.path.basename(path):20s} {size:>10,} bytes")

    print("\n=== COUNTS ===")
    print(f"  questions: {len(selected_qids)}")
    print(f"  pairs:     {len(pairs_list)}")
    print(f"  subjects:  {len(subjects_out)}")
    print(f"  links:     {len(subject_links_out)}")
    print(f"  missing pair_ids: {missing_count}")

    # referential integrity
    pair_id_set = {r["pair_id"] for r in pairs_list}
    bad_ans = [
        (mc["question_id"], pid)
        for mc in memory_cases
        for pid in mc["answer_pair_ids"]
        if pid not in pair_id_set
    ]
    bad_links = [l for l in subject_links_out if l["pair_id"] not in pair_id_set]
    print("\n=== REFERENTIAL INTEGRITY ===")
    print(f"  answer_pair_ids not in pairs.json: {len(bad_ans)}")
    print(f"  subject_link pair_ids not in pairs.json: {len(bad_links)}")
    if bad_ans:
        print("  !! bad answer refs:", bad_ans[:10])
    if bad_links:
        print("  !! bad link refs:", bad_links[:10])

    # null timestamps?
    null_ts = [r["pair_id"] for r in pairs_list if not r["timestamp"]]
    print(f"  pairs with null/unparsed timestamp: {len(null_ts)}")
    if null_ts:
        print("    e.g.", null_ts[:5])

    # sample memory case
    print("\n=== SAMPLE MEMORY CASE ===")
    sample = next((m for m in memory_cases if m["answer_pair_ids"]), memory_cases[0])
    print(f"  question_id:     {sample['question_id']}")
    print(f"  question_type:   {sample['question_type']}")
    print(f"  query:           {sample['query'][:100]}")
    print(f"  answer:          {str(sample['answer'])[:100]}")
    print(f"  #answer_pair_ids: {len(sample['answer_pair_ids'])}")


if __name__ == "__main__":
    main()
