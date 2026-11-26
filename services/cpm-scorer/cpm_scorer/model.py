"""Model loading and the pointwise relevance interface (contract §2.4).

The base is loaded text-only with `Qwen3_5ForCausalLM`. A LoRA adapter is
merged into float32 weights before the cast to the run dtype; a full
fine-tune replaces the base tensors after `check_full` accepted its names and
shapes. The tokenizer is always the base's.

Relevance score = logit("yes") - logit("no") at the first assistant position,
where each logit is the final hidden state (after the model's final norm) dot
the tied lm_head row, computed in float32.

Determinism: fixed batch size 32, fixed order, right padding (Gated DeltaNet
layers are causal recurrences, so trailing pads never reach a real token),
`torch.use_deterministic_algorithms(True)` and SDPA's math backend. The
Gated DeltaNet short convolution runs through `shift_causal_conv1d` on every
device: transformers' reference path calls a depthwise `F.conv1d` that is
about 200x slower on CPU, and pinning one implementation keeps the CPU
practice path and the GPU scorer on the same kernel.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cpm_scorer.bases import BASE_ALLOW_PATTERNS, Base

BATCH_SIZE = 32
QUERY_MAX_TOKENS = 256
CANDIDATE_MAX_TOKENS = 768
# One seed renders the whole catalog (~1.5k tools) plus each case's memories
# and queries, which fits; a long-lived serve process must not grow forever.
TRUNC_CACHE_MAX = 4096
SYSTEM_PROMPT = (
    "Judge whether the Document is needed to answer the Query for this user. "
    'Answer only "yes" or "no".'
)
YES, NO = "yes", "no"
# Optimised kernels change numerics; the pinned path is pure torch.
_UNPINNED_KERNEL_PACKAGES = ("fla", "causal_conv1d", "kernels")


def user_message(query: str, candidate: str) -> str:
    return f"<Query>{query}</Query>\n<Document>{candidate}</Document>"


def shift_causal_conv1d(
    hidden_states: Any,
    weight: Any,
    bias: Any = None,
    activation: str | None = None,
    **_: Any,
) -> Any:
    """Depthwise causal conv as K shifted multiply-adds, summed in k order.

    hidden_states: (batch, channels, seq); weight: (channels, K).
    """
    import torch.nn.functional as F

    k = weight.shape[-1]
    seq = hidden_states.shape[-1]
    x = F.pad(hidden_states.to(weight.dtype), (k - 1, 0))
    out = x[..., 0:seq] * weight[:, 0:1]
    for i in range(1, k):
        out = out + x[..., i : i + seq] * weight[:, i : i + 1]
    if bias is not None:
        out = out + bias[:, None]
    if activation is not None:
        if activation not in ("silu", "swish"):
            raise ValueError(f"unsupported activation {activation}")
        out = F.silu(out)
    return out.to(hidden_states.dtype)


def configure_determinism(device: str) -> None:
    """Process-wide deterministic settings. Call before any CUDA work."""
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch

    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True)
    if device.startswith("cuda"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False


def run_dtype(device: str) -> Any:
    import torch

    return torch.bfloat16 if device.startswith("cuda") else torch.float32


def _pin_kernels() -> None:
    present = [p for p in _UNPINNED_KERNEL_PACKAGES if importlib.util.find_spec(p)]
    if present:
        raise RuntimeError(
            f"unpinned kernel packages installed: {present}; the scorer runs "
            "the pure-torch path only"
        )
    import transformers.models.qwen3_5.modeling_qwen3_5 as mq

    mq.causal_conv1d_fn = shift_causal_conv1d


def download_base(base: Base, cache_dir: str | None = None) -> Path:
    from huggingface_hub import snapshot_download

    return Path(
        snapshot_download(
            base.repo,
            revision=base.revision,
            allow_patterns=BASE_ALLOW_PATTERNS,
            cache_dir=cache_dir,
        )
    )


def load_tokenizer(base_dir: Path) -> Any:
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(str(base_dir))
    for word in (YES, NO):
        ids = tok.encode(word, add_special_tokens=False)
        if len(ids) != 1:
            raise RuntimeError(f"{word!r} is not a single token: {ids}")
    return tok


def load_base_model(base_dir: Path) -> Any:
    """Text-only float32 base. Fails if any text weight is missing."""
    import torch
    from transformers import Qwen3_5ForCausalLM

    _pin_kernels()
    model, info = Qwen3_5ForCausalLM.from_pretrained(
        str(base_dir),
        dtype=torch.float32,
        attn_implementation="sdpa",
        output_loading_info=True,
    )
    if info["missing_keys"] or info["mismatched_keys"]:
        raise RuntimeError(f"text-only load incomplete: {info}")
    model.eval()
    return model


def base_tensor_shapes(model: Any) -> dict[str, tuple[int, ...]]:
    """Tensor names and shapes a full fine-tune must match (tied lm_head
    excluded, as `save_pretrained` writes it)."""
    sd = model.state_dict()
    tied = getattr(model.config, "tie_word_embeddings", False)
    return {
        k: tuple(v.shape) for k, v in sd.items() if not (tied and k == "lm_head.weight")
    }


def apply_lora(model: Any, adapter_dir: Path) -> Any:
    from peft import PeftModel

    peft_model = PeftModel.from_pretrained(model, str(adapter_dir), is_trainable=False)
    return peft_model.merge_and_unload()


def apply_full(model: Any, shard_paths: Sequence[Path]) -> Any:
    from safetensors.torch import load_file

    sd: dict[str, Any] = {}
    for p in shard_paths:
        sd.update(load_file(str(p)))
    missing, unexpected = model.load_state_dict(sd, strict=False)
    allowed_missing = {"lm_head.weight"} if model.config.tie_word_embeddings else set()
    if set(missing) - allowed_missing or unexpected:
        raise RuntimeError(f"full weights mismatch: {missing} {unexpected}")
    return model


@dataclass
class Prepared:
    input_ids: list[list[int]]
    tokens: int


class RelevanceScorer:
    def __init__(self, model: Any, tokenizer: Any, device: str):
        import torch

        _pin_kernels()
        self.device = device
        self.dtype = run_dtype(device)
        self.model = model.to(device=device, dtype=self.dtype)
        self.model.eval()
        self.tok = tokenizer
        self.pad_id = (
            tokenizer.pad_token_id
            if tokenizer.pad_token_id is not None
            else tokenizer.eos_token_id
        )
        (yes,) = tokenizer.encode(YES, add_special_tokens=False)
        (no,) = tokenizer.encode(NO, add_special_tokens=False)
        head = self.model.get_output_embeddings().weight
        self.head_rows = head[[yes, no]].detach().to(torch.float32)
        self._trunc_cache: dict[tuple[str, int], str] = {}

    def truncate(self, text: str, max_tokens: int) -> str:
        key = (text, max_tokens)
        hit = self._trunc_cache.get(key)
        if hit is None:
            ids = self.tok(text, add_special_tokens=False)["input_ids"]
            hit = text if len(ids) <= max_tokens else self.tok.decode(ids[:max_tokens])
            if len(self._trunc_cache) >= TRUNC_CACHE_MAX:
                # Oldest first: dicts keep insertion order.
                del self._trunc_cache[next(iter(self._trunc_cache))]
            self._trunc_cache[key] = hit
        return hit

    def render(self, query: str, candidate: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": user_message(
                    self.truncate(query, QUERY_MAX_TOKENS),
                    self.truncate(candidate, CANDIDATE_MAX_TOKENS),
                ),
            },
        ]
        return self.tok.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        enc = self.tok(list(texts), add_special_tokens=False)["input_ids"]
        return [len(x) for x in enc]

    def score(
        self,
        pairs: Sequence[tuple[str, str]],
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[float], int]:
        """Score (query, candidate) pairs in the given order, in fixed batches
        of 32. Returns (scores, model input tokens)."""
        import torch
        from torch.nn.attention import SDPBackend, sdpa_kernel

        scores: list[float] = []
        total_tokens = 0
        for start in range(0, len(pairs), BATCH_SIZE):
            chunk = pairs[start : start + BATCH_SIZE]
            texts = [self.render(q, c) for q, c in chunk]
            ids = self.tok(texts, add_special_tokens=False)["input_ids"]
            lengths = [len(x) for x in ids]
            total_tokens += sum(lengths)
            width = max(lengths)
            input_ids = torch.full((len(ids), width), self.pad_id, dtype=torch.long)
            mask = torch.zeros((len(ids), width), dtype=torch.long)
            for i, row in enumerate(ids):
                input_ids[i, : len(row)] = torch.tensor(row, dtype=torch.long)
                mask[i, : len(row)] = 1
            with torch.inference_mode(), sdpa_kernel(SDPBackend.MATH):
                hidden = self.model.model(
                    input_ids=input_ids.to(self.device),
                    attention_mask=mask.to(self.device),
                ).last_hidden_state
                last = torch.tensor([n - 1 for n in lengths], device=hidden.device)
                h = hidden[torch.arange(len(ids), device=hidden.device), last]
                logits = h.to(torch.float32) @ self.head_rows.to(h.device).T
                diff = (logits[:, 0] - logits[:, 1]).cpu()
            scores.extend(float(x) for x in diff.tolist())
            if progress:
                progress(min(start + BATCH_SIZE, len(pairs)), len(pairs))
        return scores, total_tokens


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
