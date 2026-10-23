"""Exact port of the product's BERT WordPiece tokenizer.

Source of truth: backend ``pkg/services/retrieval/crossencoder/tokenizer.go``.
The product's tokenizer is parity-tested against HuggingFace's uncased
``BertTokenizer``; this port follows the Go code line by line rather than
HuggingFace, so a miner's model is scored on exactly the token ids the product
would feed it. Where Python's string methods differ from Go's, the Go
behaviour is reproduced explicitly (see ``_lower`` and ``_GO_SPACE``).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from dittobench_reranker.contract import SPECIAL_TOKEN_IDS

CLS_ID = SPECIAL_TOKEN_IDS["[CLS]"]
SEP_ID = SPECIAL_TOKEN_IDS["[SEP]"]
PAD_ID = SPECIAL_TOKEN_IDS["[PAD]"]
UNK_ID = SPECIAL_TOKEN_IDS["[UNK]"]
MAX_INPUT_CHARS_PER_WORD = 100

# Go's unicode.IsSpace: the Unicode White_Space property. Python's str.isspace
# also accepts U+001C..U+001F, which Go does not.
_GO_SPACE = frozenset(
    [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680]
    + list(range(0x2000, 0x200B))
    + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
)

_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x20000, 0x2A6DF),
    (0x2A700, 0x2B73F),
    (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF),
    (0xF900, 0xFAFF),
    (0x2F800, 0x2FA1F),
)


def _is_space(ch: str) -> bool:
    return ord(ch) in _GO_SPACE


def _is_control(ch: str) -> bool:
    if ch in "\t\n\r":
        return False
    return unicodedata.category(ch) in ("Cc", "Cf")


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _lower(token: str) -> str:
    """Go strings.ToLower: per-rune simple case mapping.

    Python's str.lower applies full and context-sensitive mappings (final
    sigma, U+0130 to two code points). Lowering one character at a time removes
    the context; U+0130 is the only character whose full mapping is longer than
    one code point, and Go maps it to plain "i".
    """
    out = []
    for ch in token:
        if ch == "İ":
            out.append("i")
            continue
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


def _clean_text(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        out.append(" " if _is_space(ch) else ch)
    return "".join(out)


def _tokenize_chinese_chars(text: str) -> str:
    return "".join(f" {ch} " if _is_cjk(ch) else ch for ch in text)


def _strip_accents(token: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", token)
        if unicodedata.category(ch) != "Mn"
    )


def _split_on_punc(token: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for ch in token:
        if _is_punctuation(ch):
            if cur:
                out.append("".join(cur))
                cur = []
            out.append(ch)
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _fields(text: str) -> list[str]:
    """Go strings.FieldsFunc(text, unicode.IsSpace)."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text:
        if _is_space(ch):
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def basic_tokenize(text: str) -> list[str]:
    text = _tokenize_chinese_chars(_clean_text(text))
    out: list[str] = []
    for tok in _fields(text):
        out.extend(_split_on_punc(_strip_accents(_lower(tok))))
    return out


class WordPiece:
    """Uncased BERT WordPiece tokenizer built from a vocab.txt."""

    def __init__(self, vocab: dict[str, int]):
        self.vocab = vocab
        self._cache: dict[str, list[int]] = {}

    @classmethod
    def from_text(cls, text: str) -> WordPiece:
        # bufio.Scanner line semantics: split on "\n", drop one trailing "\r",
        # and no empty token after a final newline. A repeated token keeps the
        # last id, like a Go map assignment.
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        vocab: dict[str, int] = {}
        for i, line in enumerate(lines):
            vocab[line.rstrip("\r\n")] = i
        return cls(vocab)

    @classmethod
    def from_file(cls, path: str | Path) -> WordPiece:
        return cls.from_text(Path(path).read_bytes().decode("utf-8"))

    def _word_piece(self, token: str) -> list[int]:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        if len(token) > MAX_INPUT_CHARS_PER_WORD:
            return [UNK_ID]
        out: list[int] = []
        start = 0
        n = len(token)
        while start < n:
            end = n
            cur = -1
            while start < end:
                sub = token[start:end]
                if start > 0:
                    sub = "##" + sub
                found = self.vocab.get(sub)
                if found is not None:
                    cur = found
                    break
                end -= 1
            if cur < 0:
                out = [UNK_ID]
                break
            out.append(cur)
            start = end
        if len(self._cache) < 200_000:
            self._cache[token] = out
        return out

    def tokenize_to_ids(self, text: str) -> list[int]:
        ids: list[int] = []
        for word in basic_tokenize(text):
            ids.extend(self._word_piece(word))
        return ids

    def encode_pair(
        self, query: str, doc: str, max_len: int
    ) -> tuple[list[int], list[int]]:
        """[CLS] q [SEP] d [SEP], longest-first truncation to max_len."""
        q = self.tokenize_to_ids(query)
        d = self.tokenize_to_ids(doc)
        budget = max(max_len - 3, 0)
        while len(q) + len(d) > budget:
            if len(q) > len(d):
                q.pop()
            else:
                d.pop()
        ids = [CLS_ID, *q, SEP_ID, *d, SEP_ID]
        types = [0] * (len(q) + 2) + [1] * (len(d) + 1)
        return ids, types
