"""Shared pieces of the CPM v2 span-extraction PoC.

Interface: the base chat template (thinking disabled) with the query first and
the raw document after it, so a causal decoder has read the query before any
document token. A span head, `Linear(hidden, 2)`, reads the final hidden state
(after the model's last norm) at every document token and gives a start and an
end logit. The predicted span is the highest-scoring (start <= end) pair of
document tokens with at most MAX_SPAN_TOKENS tokens, decoded in float64 on the
CPU with ties to the earliest start, then the earliest end. A prediction is a
token range of the input, so it is verbatim by construction.

Gold spans and predictions are compared as sets of document token indices of
the base tokenizer (`metrics`).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SPAN_SYSTEM_PROMPT = (
    "Find the exact span of the Document that answers the Query for this user."
)
QUERY_MAX_TOKENS = 256
MAX_SPAN_TOKENS = 32
BUDGET_TOKENS = 2048
HEAD_FILE = "span_head.safetensors"
DOC_OPEN, DOC_CLOSE = "<Document>", "</Document>"


@dataclass(frozen=True)
class Encoded:
    input_ids: list[int]
    doc_tokens: tuple[int, int]  # [first, last] input positions of the doc
    offsets: list[tuple[int, int]]  # per input token, in document chars
    gold: tuple[int, int]  # [start, end] input positions, inclusive


def truncate(tok, text: str, max_tokens: int) -> str:
    ids = tok(text, add_special_tokens=False)["input_ids"]
    return text if len(ids) <= max_tokens else tok.decode(ids[:max_tokens])


def render(tok, query: str, doc: str, system: str = SPAN_SYSTEM_PROMPT):
    """(prompt text, char offset of the document inside it)."""
    user = (
        f"<Query>{truncate(tok, query, QUERY_MAX_TOKENS)}</Query>\n"
        f"{DOC_OPEN}{doc}{DOC_CLOSE}"
    )
    text = tok.apply_chat_template(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    start = text.index(DOC_OPEN + doc + DOC_CLOSE) + len(DOC_OPEN)
    return text, start


def encode(tok, ex: dict, system: str = SPAN_SYSTEM_PROMPT) -> Encoded:
    text, d0 = render(tok, ex["query"], ex["doc"], system)
    d1 = d0 + len(ex["doc"])
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offs = enc["input_ids"], enc["offset_mapping"]
    doc_pos = [i for i, (a, b) in enumerate(offs) if b > a and a < d1 and b > d0]
    first, last = doc_pos[0], doc_pos[-1]
    if doc_pos != list(range(first, last + 1)):
        raise RuntimeError(f"document tokens not contiguous in {ex['case_id']}")
    rel = [(a - d0, b - d0) for a, b in offs]
    gs, ge = ex["char_start"], ex["char_end"]
    span = [i for i in doc_pos if rel[i][1] > gs and rel[i][0] < ge]
    return Encoded(ids, (first, last), rel, (span[0], span[-1]))


def char_to_tokens(e: Encoded, start: int, end: int) -> tuple[int, int] | None:
    """Document token range covering document chars [start, end)."""
    first, last = e.doc_tokens
    span = [
        i
        for i in range(first, last + 1)
        if e.offsets[i][1] > start and e.offsets[i][0] < end
    ]
    return (span[0], span[-1]) if span else None


def decode(start_logits, end_logits, e: Encoded, max_len: int = MAX_SPAN_TOKENS):
    """Best (start, end) over document tokens; float64, deterministic ties."""
    import torch

    first, last = e.doc_tokens
    s = start_logits[first : last + 1].detach().cpu().to(torch.float64)
    t = end_logits[first : last + 1].detach().cpu().to(torch.float64)
    n = s.shape[0]
    neg = torch.full((n,), float("-inf"), dtype=torch.float64)
    cols = []
    for k in range(max_len):
        shifted = torch.cat([t[k:], neg[:k]]) if k else t
        cols.append(s + shifted)
    grid = torch.stack(cols, 1)  # grid[i, k] = s[i] + t[i + k]
    flat = int(torch.argmax(grid.reshape(-1)))  # first maximum in row-major
    i, k = divmod(flat, max_len)
    return first + i, first + i + k


class SpanHead:
    """Factory for the head module (torch imported lazily)."""

    @staticmethod
    def build(hidden: int, seed: int = 0):
        import torch

        g = torch.Generator().manual_seed(seed)
        head = torch.nn.Linear(hidden, 2)
        with torch.no_grad():
            head.weight.copy_(torch.randn(head.weight.shape, generator=g) * 0.02)
            head.bias.zero_()
        return head


def span_logits(decoder, head, input_ids: Sequence[int], device: str):
    """Start and end logits (float32) for one sequence; decoder = model.model."""
    import torch

    ids = torch.tensor([list(input_ids)], dtype=torch.long, device=device)
    hidden = decoder(input_ids=ids).last_hidden_state[0]
    logits = head(hidden.float())
    return logits[:, 0], logits[:, 1]


def span_loss(start_logits, end_logits, e: Encoded):
    import torch

    first, last = e.doc_tokens
    s = start_logits[first : last + 1]
    t = end_logits[first : last + 1]
    gs = torch.tensor([e.gold[0] - first], device=s.device)
    ge = torch.tensor([e.gold[1] - first], device=s.device)
    ce = torch.nn.functional.cross_entropy
    return 0.5 * (ce(s[None], gs) + ce(t[None], ge))


def example_metrics(pred: tuple[int, int] | None, e: Encoded) -> dict[str, Any]:
    gold = set(range(e.gold[0], e.gold[1] + 1))
    got = set(range(pred[0], pred[1] + 1)) if pred else set()
    inter = len(gold & got)
    p = inter / len(got) if got else 0.0
    r = inter / len(gold)
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {
        "exact": pred is not None and tuple(pred) == e.gold,
        "precision": p,
        "recall": r,
        "f1": f1,
        "iou": inter / len(gold | got),
        "contained": gold <= got,
        "tokens": len(got),
        "budget_share": len(got) / BUDGET_TOKENS,
    }


METRIC_KEYS = ("exact", "precision", "recall", "f1", "iou", "contained", "tokens")


def summarize(rows: Sequence[dict], resamples: int = 2000, seed: int = 20261122):
    """Means with 95% percentile bootstrap intervals over examples."""
    n = len(rows)
    out: dict[str, Any] = {"n": n}
    rng = random.Random(seed)
    idx = [[rng.randrange(n) for _ in range(n)] for _ in range(resamples)]
    for k in METRIC_KEYS:
        vals = [float(r[k]) for r in rows]
        mean = sum(vals) / n
        boots = sorted(sum(vals[i] for i in ix) / n for ix in idx)
        lo = boots[int(0.025 * resamples)]
        hi = boots[int(0.975 * resamples) - 1]
        out[k] = {"mean": mean, "ci95": [lo, hi]}
    out["budget_share"] = out["tokens"]["mean"] / BUDGET_TOKENS
    return out
