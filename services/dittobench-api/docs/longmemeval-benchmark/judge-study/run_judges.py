"""Run a panel of candidate judges over LongMemEval probes.

Reimplements LongMemEval evaluate_qa.py (rev 9e0b455f) EXACTLY:
  - the five get_anscheck_prompt templates + the abstention template
  - kwargs: temperature=0, max_tokens=10, n=1
  - label extraction: 'yes' in response.lower()
Adds only: raw-response capture (to measure parse ambiguity) and usage/cost.
"""
import json, os, sys, time, hashlib, threading, argparse, collections
import urllib.request, urllib.error

API = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ["OPENROUTER_API_KEY"]

def get_anscheck_prompt(task, question, answer, response, abstention=False):
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        else:
            raise NotImplementedError
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    return template.format(question, answer, response)

def call(model, prompt, extra=None, tries=6):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "n": 1, "temperature": 0, "max_tokens": 10,
            "usage": {"include": True}}
    if extra:
        body.update(extra)
    data = json.dumps(body).encode()
    last = None
    for a in range(tries):
        req = urllib.request.Request(API, data=data, headers={
            "Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            if "choices" not in d or not d["choices"]:
                last = "no-choices:" + json.dumps(d)[:200]
                time.sleep(2 ** a); continue
            msg = d["choices"][0].get("message") or {}
            txt = (msg.get("content") or "")
            u = d.get("usage") or {}
            det = u.get("completion_tokens_details") or {}
            return {"raw": txt, "cost": u.get("cost", 0.0),
                    "provider": d.get("provider"),
                    "reasoning_tokens": det.get("reasoning_tokens"),
                    "completion_tokens": u.get("completion_tokens"),
                    "finish": d["choices"][0].get("finish_reason")}
        except urllib.error.HTTPError as e:
            last = "http%d:%s" % (e.code, e.read()[:200].decode("utf8", "replace"))
            if e.code in (429, 500, 502, 503, 524): time.sleep(min(2 ** a, 30)); continue
            break
        except Exception as e:
            last = "%s:%s" % (type(e).__name__, e); time.sleep(min(2 ** a, 30))
    return {"error": last}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", required=True)
    ap.add_argument("--refs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stratify", action="store_true")
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()

    refs = {e["question_id"]: e for e in json.load(open(a.refs))}
    probes = [json.loads(l) for l in open(a.probes)]
    if a.limit:
        if a.stratify:
            by = collections.defaultdict(list)
            for p in probes: by[p["probe"]].append(p)
            per = max(1, a.limit // len(by)); sel = []
            for k in sorted(by): sel.extend(by[k][:per])
            probes = sel
        else:
            probes = probes[:a.limit]

    done = set()
    if os.path.exists(a.out):
        for l in open(a.out):
            try:
                r = json.loads(l); done.add((r["model_arm"], r["probe_id"], r["rep"]))
            except Exception: pass
    print("probes=%d models=%d reps=%d already=%d" % (
        len(probes), len(a.models.split(";")), a.reps, len(done)), flush=True)

    arms = []
    for spec in a.models.split(";"):
        if "|" in spec:
            arm, model, prov = spec.split("|", 2)
            arms.append((arm, model, json.loads(prov) if prov.strip() else None))
        else:
            arms.append((spec, spec, None))

    jobs = [(arm, model, prov, p, rep)
            for arm, model, prov in arms
            for p in probes
            for rep in range(a.reps)
            if (arm, p["probe_id"], rep) not in done]
    print("jobs=%d" % len(jobs), flush=True)

    lock = threading.Lock(); fh = open(a.out, "a")
    state = {"n": 0, "cost": 0.0, "err": 0}
    def work(job):
        arm, model, prov, p, rep = job
        e = refs[p["question_id"]]
        prompt = get_anscheck_prompt(p["question_type"], e["question"],
                                     str(e["answer"]), p["hypothesis"],
                                     abstention=p["abstention"])
        r = call(model, prompt, prov)
        rec = {"model_arm": arm, "model": model, "probe_id": p["probe_id"],
               "probe": p["probe"], "question_type": p["question_type"],
               "abstention": p["abstention"], "gold": p["gold"], "rep": rep}
        if "error" in r:
            rec["error"] = r["error"]
        else:
            rec["raw"] = r["raw"]; rec["label"] = "yes" in r["raw"].lower()
            rec["cost"] = r.get("cost", 0.0); rec["provider"] = r.get("provider")
            rec["finish"] = r.get("finish")
            rec["reasoning_tokens"] = r.get("reasoning_tokens")
            rec["completion_tokens"] = r.get("completion_tokens")
        with lock:
            fh.write(json.dumps(rec, sort_keys=True) + "\n"); fh.flush()
            state["n"] += 1; state["cost"] += rec.get("cost", 0.0) or 0.0
            if "error" in rec: state["err"] += 1
            if state["n"] % 200 == 0:
                print("  %d/%d  cost=$%.3f  err=%d" % (
                    state["n"], len(jobs), state["cost"], state["err"]), flush=True)

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(work, jobs))
    fh.close()
    print("DONE n=%d cost=$%.4f errors=%d" % (state["n"], state["cost"], state["err"]))

main()
