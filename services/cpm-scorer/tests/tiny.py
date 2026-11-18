"""A tiny randomly initialised Qwen3.5 text model and word-level tokenizer,
built in-process so tests never download a checkpoint."""

from __future__ import annotations

import torch
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import PreTrainedTokenizerFast, Qwen3_5ForCausalLM, Qwen3_5TextConfig

SPECIALS = ["<|endoftext|>", "<|im_start|>", "<|im_end|>", "<think>", "</think>"]
CHAT_TEMPLATE = (
    "{% for m in messages %}<|im_start|> {{ m.role }} {{ m.content }} <|im_end|> "
    "{% endfor %}{% if add_generation_prompt %}<|im_start|> assistant "
    "{% if enable_thinking is defined and not enable_thinking %}"
    "<think> </think> {% endif %}{% endif %}"
)
WORDS = [
    "yes",
    "no",
    "system",
    "user",
    "assistant",
    "Judge",
    "whether",
    "the",
    "Document",
    "is",
    "needed",
    "to",
    "answer",
    "Query",
    "for",
    "this",
    "user.",
    "Answer",
    "only",
]


def tiny_tokenizer(extra_words: list[str] | None = None) -> PreTrainedTokenizerFast:
    vocab: dict[str, int] = {}
    for w in [*SPECIALS, "[UNK]", *WORDS, *(extra_words or [])]:
        vocab.setdefault(w, len(vocab))
    tk = Tokenizer(models.WordLevel(vocab=vocab, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tok = PreTrainedTokenizerFast(
        tokenizer_object=tk,
        unk_token="[UNK]",
        pad_token="<|endoftext|>",
        eos_token="<|im_end|>",
        additional_special_tokens=SPECIALS[1:],
    )
    tok.chat_template = CHAT_TEMPLATE
    return tok


def tiny_config(vocab_size: int) -> Qwen3_5TextConfig:
    return Qwen3_5TextConfig(
        vocab_size=vocab_size,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=4,
        layer_types=["linear_attention"] * 3 + ["full_attention"],
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=16,
        linear_num_key_heads=2,
        linear_num_value_heads=2,
        linear_key_head_dim=8,
        linear_value_head_dim=8,
        linear_conv_kernel_dim=4,
        tie_word_embeddings=True,
        rope_parameters={
            "rope_type": "default",
            "rope_theta": 10000.0,
            "partial_rotary_factor": 0.5,
            "mrope_section": [2, 1, 1],
            "mrope_interleaved": True,
        },
        pad_token_id=0,
    )


def tiny_model(vocab_size: int, seed: int = 0) -> Qwen3_5ForCausalLM:
    torch.manual_seed(seed)
    model = Qwen3_5ForCausalLM(tiny_config(vocab_size))
    model.eval()
    return model
