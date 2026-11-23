"""Multi-span heads for the CPM v2 PoC: a fixed buffer of K span tuples.

Output contract (both heads): `K` slots of `(start, end)` document-token
ranges in document order, non-overlapping, padded with `(-1, -1)`. An all-empty
buffer is "no answer": nothing in the document is relevant. Decoding is done
by the scorer in float64 on the CPU, with ties to the earliest position, so a
prediction is a pure function of the logits.

Two ways to fill the buffer from one forward pass (same template as
`span_model`, query before the document):

- `slots`: `Linear(hidden, 2K)`. Slot k gives start and end logits over the
  document tokens plus one null position, the last input token (it has read
  the whole document). Gold spans are assigned to slots in document order;
  unused slots point at null. Decoding is sequential: slot k takes the best
  span that starts after slot k-1's end, or null, and the first null ends the
  buffer.
  With `matching="set"`, gold spans go to the slots that already score them
  best (an exact minimum-cost assignment, DETR-style), and decoding is per
  slot: every non-null slot proposes its best span, and the highest-scoring
  non-overlapping proposals fill the buffer in document order.
  With `count=True` the head has K + 1 more columns: at the null position they
  predict how many spans the document holds (0..K). Decoding then keeps the
  top n̂ non-overlapping slot proposals, ranked by how far each beats its
  slot's null score, and n̂ = 0 is "no answer".
- `pointer`: `Linear(hidden, 1 + L)`. At every document token j, column 0 is an
  "a relevant span ends here" logit and columns 1..L point back to the span's
  start (offset 0..L-1). A causal model has read the whole span at its end
  token, so the start never has to be guessed from left context alone.
  Decoding: every j with end logit > 0 proposes (j - argmax offset, j); the
  top K non-overlapping proposals by end logit fill the buffer.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import span_model as sm

K_SLOTS = 12
HEADS = ("slots", "pointer")
MATCHINGS = ("ordered", "set")
NULL = (-1, -1)
END_BIAS_INIT = -7.0  # about 1 / 1,100: the end-token prior of a document


@dataclass(frozen=True)
class EncodedMulti:
    input_ids: list[int]
    doc_tokens: tuple[int, int]  # [first, last] input positions of the doc
    offsets: list[tuple[int, int]]  # per input token, in document chars
    gold: tuple[tuple[int, int], ...]  # sorted, disjoint, inclusive

    @property
    def null_pos(self) -> int:
        return len(self.input_ids) - 1


def encode(tok, ex: dict, level: str, max_span: int) -> EncodedMulti:
    text, d0 = sm.render(tok, ex["query"], ex["doc"])
    d1 = d0 + len(ex["doc"])
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offs = enc["input_ids"], enc["offset_mapping"]
    doc_pos = [i for i, (a, b) in enumerate(offs) if b > a and a < d1 and b > d0]
    first, last = doc_pos[0], doc_pos[-1]
    if doc_pos != list(range(first, last + 1)):
        raise RuntimeError(f"document tokens not contiguous in {ex['case_id']}")
    rel = [(a - d0, b - d0) for a, b in offs]
    spans: list[list[int]] = []
    for gs, ge in ex["gold"][level]:
        tk = [i for i in doc_pos if rel[i][1] > gs and rel[i][0] < ge]
        a, b = tk[0], tk[-1]
        if spans and a <= spans[-1][1]:  # two char spans share a token
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    for a, b in spans:
        if b - a + 1 > max_span:
            raise RuntimeError(f"gold span over {max_span} tokens in {ex['case_id']}")
    return EncodedMulti(ids, (first, last), rel, tuple((a, b) for a, b in spans))


def build_head(
    kind: str, hidden: int, k: int, max_span: int, seed: int = 0, count: bool = False
):
    import torch

    out = 2 * k + (k + 1 if count else 0) if kind == "slots" else 1 + max_span
    g = torch.Generator().manual_seed(seed)
    head = torch.nn.Linear(hidden, out)
    with torch.no_grad():
        head.weight.copy_(torch.randn(head.weight.shape, generator=g) * 0.02)
        head.bias.zero_()
        if kind == "pointer":
            head.bias[0] = END_BIAS_INIT
    return head


def head_logits(decoder, head, input_ids: Sequence[int], device: str):
    import torch

    ids = torch.tensor([list(input_ids)], dtype=torch.long, device=device)
    hidden = decoder(input_ids=ids).last_hidden_state[0]
    return head(hidden.float())  # [T, out]


def _slot_positions(e: EncodedMulti):
    first, last = e.doc_tokens
    return [*range(first, last + 1), e.null_pos]


def match_slots(cost, null_cost) -> list[int]:
    """Exact min-cost assignment of every gold span to a distinct slot.

    cost[s][g] is slot s's loss for gold g, null_cost[s] its loss for null.
    Returns, per slot, the gold index or -1. A DP over (slot, set of golds
    used); ties go to the lower slot and gold index, so it is deterministic.
    """
    k, n_gold = len(cost), len(cost[0]) if cost else 0
    full = (1 << n_gold) - 1
    inf = float("inf")
    dp = [[inf] * (full + 1) for _ in range(k + 1)]
    choice = [[-1] * (full + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for s in range(k):
        for mask in range(full + 1):
            base = dp[s][mask]
            if base == inf:
                continue
            c = base + null_cost[s]
            if c < dp[s + 1][mask]:
                dp[s + 1][mask], choice[s + 1][mask] = c, -1
            for g in range(n_gold):
                if mask >> g & 1:
                    continue
                nm = mask | 1 << g
                c = base + cost[s][g]
                if c < dp[s + 1][nm]:
                    dp[s + 1][nm], choice[s + 1][nm] = c, g
    out, mask = [-1] * k, full
    for s in range(k, 0, -1):
        g = choice[s][mask]
        out[s - 1] = g
        if g >= 0:
            mask ^= 1 << g
    return out


def loss(
    kind: str,
    logits,
    e: EncodedMulti,
    k: int,
    max_span: int,
    matching: str = "ordered",
):
    import torch

    ce = torch.nn.functional.cross_entropy
    first, last = e.doc_tokens
    n = last - first + 1
    if kind == "slots":
        pos = torch.tensor(_slot_positions(e), device=logits.device)
        sub = logits[pos][:, : 2 * k]  # [n + 1, 2k]; row n is null
        count_loss = 0.0
        if logits.shape[1] > 2 * k:  # count columns at the null position
            target = torch.tensor([min(len(e.gold), k)], device=logits.device)
            count_loss = ce(logits[e.null_pos, 2 * k :][None], target)
        if matching == "set" and e.gold:
            with torch.no_grad():
                ls = torch.log_softmax(sub[:, 0::2].T.float(), -1).cpu()
                le = torch.log_softmax(sub[:, 1::2].T.float(), -1).cpu()
            cost = [
                [-float(ls[s, a - first] + le[s, b - first]) for a, b in e.gold]
                for s in range(k)
            ]
            null_cost = [-float(ls[s, n] + le[s, n]) for s in range(k)]
            assign = match_slots(cost, null_cost)
            tgt_s = [e.gold[g][0] - first if g >= 0 else n for g in assign]
            tgt_e = [e.gold[g][1] - first if g >= 0 else n for g in assign]
        else:
            tgt_s = [a - first for a, _ in e.gold] + [n] * (k - len(e.gold))
            tgt_e = [b - first for _, b in e.gold] + [n] * (k - len(e.gold))
        ts = torch.tensor(tgt_s, device=logits.device)
        te = torch.tensor(tgt_e, device=logits.device)
        return 0.5 * (ce(sub[:, 0::2].T, ts) + ce(sub[:, 1::2].T, te)) + count_loss
    doc = logits[first : last + 1]
    end_t = torch.zeros(n, device=logits.device)
    for _, b in e.gold:
        end_t[b - first] = 1.0
    bce = torch.nn.functional.binary_cross_entropy_with_logits(
        doc[:, 0], end_t, reduction="sum"
    ) / (len(e.gold) + 1)
    if not e.gold:
        return bce
    rows, tgts = [], []
    for a, b in e.gold:
        ptr = doc[b - first, 1 : 1 + max_span]
        valid = min(max_span, b - first + 1)  # offsets that stay in the doc
        mask = torch.full_like(ptr, float("-inf"))
        mask[:valid] = 0.0
        rows.append(ptr + mask)
        tgts.append(b - a)
    ptr_ce = ce(torch.stack(rows), torch.tensor(tgts, device=logits.device))
    return bce + ptr_ce


def _same_segment(seg, max_span: int):
    """ok[i, k]: tokens i and i + k sit in the same memory (seg >= 0)."""
    import torch

    n = seg.shape[0]
    pad = torch.full((max_span,), -2, dtype=seg.dtype)
    ext = torch.cat([seg, pad])
    cols = [(ext[k : k + n] == seg) & (seg >= 0) for k in range(max_span)]
    return torch.stack(cols, 1)


def _best_span(s, t, lo: int, max_span: int, ok=None):
    """Best (i, j, score) with lo <= i <= j < i + max_span; float64 tensors.

    `ok` ([n, max_span] bool) further restricts the candidates."""
    import torch

    n = s.shape[0]
    if lo >= n:
        return None
    neg = torch.full((n,), float("-inf"), dtype=torch.float64)
    cols = [s + (torch.cat([t[k:], neg[:k]]) if k else t) for k in range(max_span)]
    grid = torch.stack(cols, 1)
    grid[:lo] = float("-inf")
    if ok is not None:
        grid[~ok] = float("-inf")
    if not bool(torch.isfinite(grid).any()):
        return None
    flat = int(torch.argmax(grid.reshape(-1)))
    i, k = divmod(flat, max_span)
    return i, i + k, float(grid[i, k])


def _fill(props, k: int, first: int):
    """Top-k non-overlapping (score desc, then position) proposals, in order."""
    taken: list[tuple[int, int]] = []
    for _, i, j in sorted(props):
        if len(taken) == k:
            break
        if any(i <= b and a <= j for a, b in taken):
            continue
        taken.append((i, j))
    return [(first + i, first + j) for i, j in sorted(taken)]


def decode(
    kind: str,
    logits,
    e: EncodedMulti,
    k: int,
    max_span: int,
    *,
    matching: str = "ordered",
    stop_at_null: bool = True,
    null_margin: float = 0.0,
    end_threshold: float = 0.0,
    seg=None,
    use_count: bool = False,
):
    """Fixed buffer of k (start, end) tuples, padded with NULL.

    - slots/ordered: slot s takes the best span after slot s-1's end unless
      null + null_margin scores at least as high; the first null ends the
      buffer (or is skipped with stop_at_null=False).
    - slots/set: every slot whose best span beats null + null_margin proposes
      it; proposals fill the buffer by score without overlaps.
    - pointer: tokens with end logit > end_threshold propose a span.
    - seg (per document token, memory index or -1 between memories) keeps every
      span inside one memory.
    - use_count (slots with count columns): keep the top n̂ proposals by
      margin over null, n̂ = argmax of the count logits; null_margin unused.
    """
    import torch

    first, last = e.doc_tokens
    n = last - first + 1
    ok = None
    if seg is not None:
        seg = torch.as_tensor(seg, dtype=torch.long)
        ok = _same_segment(seg, max_span)
    spans: list[tuple[int, int]] = []
    if kind == "slots":
        pos = torch.tensor(_slot_positions(e), device=logits.device)
        full = logits[pos].detach().cpu().to(torch.float64)
        sub = full[:, : 2 * k]
        if use_count:
            n_hat = int(torch.argmax(full[n, 2 * k :]))
            props = []
            for slot in range(k):
                s, t = sub[:n, 2 * slot], sub[:n, 2 * slot + 1]
                null = float(sub[n, 2 * slot] + sub[n, 2 * slot + 1])
                best = _best_span(s, t, 0, max_span, ok)
                if best is not None:
                    props.append((null - best[2], best[0], best[1]))
            spans = _fill(props, n_hat, first)
            return spans + [NULL] * (k - len(spans))
        lo, props = 0, []
        for slot in range(k):
            s, t = sub[:n, 2 * slot], sub[:n, 2 * slot + 1]
            null = float(sub[n, 2 * slot] + sub[n, 2 * slot + 1]) + null_margin
            best = _best_span(s, t, lo if matching == "ordered" else 0, max_span, ok)
            if best is None or best[2] <= null:
                if matching == "ordered" and stop_at_null:
                    break
                continue
            i, j, score = best
            if matching == "set":
                props.append((-score, i, j))
                continue
            spans.append((first + i, first + j))
            lo = j + 1
        if matching == "set":
            spans = _fill(props, k, first)
    else:
        doc = logits[first : last + 1].detach().cpu().to(torch.float64)
        props = []
        for j in range(n):
            if doc[j, 0] <= end_threshold:
                continue
            valid = min(max_span, j + 1)
            ptr = doc[j, 1 : 1 + valid].clone()
            if ok is not None:
                for o in range(valid):  # start j - o must share j's memory
                    if not ok[j - o, o]:
                        ptr[o] = float("-inf")
                if not bool(torch.isfinite(ptr).any()):
                    continue
            o = int(torch.argmax(ptr))
            props.append((-float(doc[j, 0]), j - o, j))
        spans = _fill(props, k, first)
    return spans + [NULL] * (k - len(spans))


def segments(e: EncodedMulti, pairs: Sequence[dict]) -> list[int]:
    """Memory index of every document token (-1 for separator tokens)."""
    first, last = e.doc_tokens
    seg = [-1] * (last - first + 1)
    for idx, p in enumerate(pairs):
        for i in range(first, last + 1):
            a, b = e.offsets[i]
            if a >= p["start"] and b <= p["end"]:
                seg[i - first] = idx
    return seg


def live(buf: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    return [(a, b) for a, b in buf if a >= 0]


def example_metrics(pred: Sequence[Sequence[int]], gold: Sequence[Sequence[int]]):
    p_sp = [tuple(x) for x in pred]
    g_sp = [tuple(x) for x in gold]
    got = {i for a, b in p_sp for i in range(a, b + 1)}
    want = {i for a, b in g_sp for i in range(a, b + 1)}
    tokens = len(got)
    out: dict[str, Any] = {
        "tokens": tokens,
        "budget_share": tokens / sm.BUDGET_TOKENS,
        "empty": not p_sp,
        "n_pred": len(p_sp),
        "n_gold": len(g_sp),
    }
    if not g_sp:
        out["null_correct"] = not p_sp
        return out
    inter = len(got & want)
    p = inter / len(got) if got else 0.0
    r = inter / len(want)
    exact = len(set(p_sp) & set(g_sp))
    sp = exact / len(p_sp) if p_sp else 0.0
    sr = exact / len(g_sp)
    out.update(
        {
            "precision": p,
            "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0.0,
            "iou": inter / len(got | want),
            "span_f1": 2 * sp * sr / (sp + sr) if sp + sr else 0.0,
            "gold_contained": sum(
                all(i in got for i in range(a, b + 1)) for a, b in g_sp
            )
            / len(g_sp),
            "all_contained": want <= got,
            "exact_set": set(p_sp) == set(g_sp),
        }
    )
    return out


POS_KEYS = (
    "f1",
    "precision",
    "recall",
    "iou",
    "span_f1",
    "gold_contained",
    "all_contained",
    "exact_set",
    "empty",
    "tokens",
)
NULL_KEYS = ("null_correct", "tokens")


def boot(rows: Sequence[dict], keys, resamples: int = 2000, seed: int = 20261122):
    n = len(rows)
    out: dict[str, Any] = {"n": n}
    if not n:
        return out
    rng = random.Random(seed)
    idx = [[rng.randrange(n) for _ in range(n)] for _ in range(resamples)]
    for key in keys:
        vals = [float(r[key]) for r in rows]
        boots = sorted(sum(vals[i] for i in ix) / n for ix in idx)
        out[key] = {
            "mean": sum(vals) / n,
            "ci95": [boots[int(0.025 * resamples)], boots[int(0.975 * resamples) - 1]],
        }
    return out


def summarize(rows: Sequence[dict]) -> dict[str, Any]:
    pos = [r for r in rows if r["n_gold"]]
    groups = {
        "positive": pos,
        "answer_single": [r for r in pos if r["kind"] == "answer" and r["n_gold"] == 1],
        "answer_multi": [r for r in pos if r["kind"] == "answer" and r["n_gold"] > 1],
        "absence": [r for r in pos if r["kind"] == "absence"],
    }
    out = {g: boot(v, POS_KEYS) for g, v in groups.items()}
    out["null"] = boot([r for r in rows if not r["n_gold"]], NULL_KEYS)
    return out
