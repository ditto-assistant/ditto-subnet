"""Allowed bases (contract §2.1).

Revisions are full commit SHAs on Hugging Face (resolved 2026-11-18 from the
short SHAs in the contract). Both checkpoints are vision-language
(`Qwen3_5ForConditionalGeneration`); the scorer loads only the text decoder
with `Qwen3_5ForCausalLM`, which maps `model.language_model.*` onto
`model.*` and drops the vision tower and the MTP head.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Base:
    base_id: str
    repo: str
    revision: str
    params: str


BASES: dict[str, Base] = {
    "qwen3.5-0.8b": Base(
        "qwen3.5-0.8b",
        "Qwen/Qwen3.5-0.8B",
        "2fc06364715b967f1860aea9cf38778875588b17",
        "0.87B",
    ),
    "qwen3.5-2b": Base(
        "qwen3.5-2b",
        "Qwen/Qwen3.5-2B",
        "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "2.27B",
    ),
}

# Attention and MLP projections, plus Qwen3.5's Gated DeltaNet
# (linear-attention) projections (contract §2.2).
ALLOWED_LORA_TARGETS = frozenset(
    {
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
        "in_proj_qkv",
        "in_proj_z",
        "in_proj_a",
        "in_proj_b",
        "out_proj",
    }
)

# Files the base checkpoint ships that the text-only loader needs.
BASE_ALLOW_PATTERNS = ["*.json", "*.safetensors", "merges.txt", "*.jinja"]


def get_base(base_id: str) -> Base:
    try:
        return BASES[base_id]
    except KeyError:
        raise ValueError(
            f"unknown base_id {base_id!r}; allowed: {sorted(BASES)}"
        ) from None
