"""Vectors ported from backend crossencoder tokenizer_test.go / reranker_test.go."""

from __future__ import annotations

import pytest

from dittobench_reranker.ranking import fuse_rrf, ndcg_at_k, recall_at_k
from dittobench_reranker.tokenizer import (
    CLS_ID,
    SEP_ID,
    UNK_ID,
    WordPiece,
    basic_tokenize,
)

# Line N -> id N-1: [PAD]=0 hello=1 world=2 test=3 ##ing=4 hi=5 !=6 cafe=7 你=8 好=9
GO_TEST_VOCAB = "[PAD]\nhello\nworld\ntest\n##ing\nhi\n!\ncafe\n你\n好\n"


@pytest.fixture()
def wp() -> WordPiece:
    return WordPiece.from_text(GO_TEST_VOCAB)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Hello WORLD", [1, 2]),
        ("testing", [3, 4]),
        ("café", [7]),
        ("hi!", [5, 6]),
        ("你好", [8, 9]),
        ("hello\t\n world", [1, 2]),
        ("qwerty", [UNK_ID]),
        ("", []),
    ],
)
def test_tokenize_to_ids_matches_go(wp: WordPiece, text: str, want: list[int]) -> None:
    assert wp.tokenize_to_ids(text) == want


def test_encode_pair_matches_go(wp: WordPiece) -> None:
    ids, types = wp.encode_pair("hello", "world", 256)
    assert ids == [CLS_ID, 1, SEP_ID, 2, SEP_ID]
    assert types == [0, 0, 0, 1, 1]


def test_encode_pair_truncates_longest_first(wp: WordPiece) -> None:
    query = "hello " * 3
    doc = "world " * 10
    # max_len 10 leaves a budget of 7: the longer doc is trimmed 10 -> 4 and
    # the query keeps all 3 tokens.
    ids, types = wp.encode_pair(query, doc, 10)
    assert ids == [CLS_ID, 1, 1, 1, SEP_ID, 2, 2, 2, 2, SEP_ID]
    assert types == [0] * 5 + [1] * 5


def test_truncation_tie_trims_the_doc(wp: WordPiece) -> None:
    # Go trims b (the doc) when both sides are equally long.
    ids, _ = wp.encode_pair("hello " * 4, "world " * 4, 3 + 7)
    assert ids == [CLS_ID, 1, 1, 1, 1, SEP_ID, 2, 2, 2, SEP_ID]


def test_long_word_is_unk(wp: WordPiece) -> None:
    assert wp.tokenize_to_ids("a" * 101) == [UNK_ID]


def test_tiny_max_len_does_not_crash(wp: WordPiece) -> None:
    ids, _ = wp.encode_pair("hello", "world", 1)
    assert ids == [CLS_ID, SEP_ID, SEP_ID]


def test_go_lowercasing_has_no_final_sigma_context() -> None:
    # Python "ΟΔΟΣ".lower() gives "οδος"; Go strings.ToLower gives "οδοσ".
    assert basic_tokenize("ΟΔΟΣ") == ["οδοσ"]
    assert basic_tokenize("İstanbul") == ["istanbul"]


def test_go_whitespace_set_differs_from_python_isspace() -> None:
    # U+001C is str.isspace() in Python but not unicode.IsSpace in Go, and it
    # is a control character, so Go's cleanText drops it entirely.
    assert basic_tokenize("a\x1cb") == ["ab"]
    assert basic_tokenize("a　b") == ["a", "b"]


def test_control_and_format_chars_are_removed() -> None:
    assert basic_tokenize("he​llo\x00 wor�ld") == ["hello", "world"]


def test_vocab_parsing_follows_bufio_scanner() -> None:
    wp = WordPiece.from_text("[PAD]\r\nhello\r\nhello\n")
    assert wp.vocab == {"[PAD]": 0, "hello": 2}


# --- fuseRRF -----------------------------------------------------------------


def test_fuse_pure_ce_order() -> None:
    assert fuse_rrf([0.1, 0.9, 0.5], 60, 1.0) == [1, 2, 0]


def test_fuse_pure_composite_order() -> None:
    assert fuse_rrf([0.1, 0.9, 0.5, 0.3], 60, 0.0) == [0, 1, 2, 3]


def test_fuse_does_not_demote_agreed_top_hit() -> None:
    assert fuse_rrf([0.99, 0.2, 0.5], 60, 0.5)[0] == 0


def test_fuse_tie_keeps_composite_order() -> None:
    assert fuse_rrf([0.5, 0.5, 0.5], 60, 0.7) == [0, 1, 2]


def test_fuse_is_permutation() -> None:
    scores = [0.3, -1.2, 4.5, 0.0, 2.2, 2.2, -0.5]
    assert sorted(fuse_rrf(scores, 60, 0.7)) == list(range(len(scores)))


def test_fuse_empty() -> None:
    assert fuse_rrf([], 60, 0.7) == []


def test_fuse_higher_ce_weight_lifts_ce_favorite() -> None:
    scores = [0.1, 0.2, 0.9, 0.3, 0.4]
    position = [fuse_rrf(scores, 60, w).index(2) for w in (0.0, 0.5, 1.0)]
    assert position[0] >= position[1] >= position[2]
    assert position[2] == 0


# --- metrics -----------------------------------------------------------------


def test_ndcg_perfect_and_worst() -> None:
    order = [f"c{i:02d}" for i in range(20)]
    assert ndcg_at_k(order, {"c00", "c01"}, 10) == pytest.approx(1.0)
    assert ndcg_at_k(order, {"c19"}, 10) == 0.0


def test_ndcg_known_value() -> None:
    import math

    order = ["a", "b", "c"]
    assert ndcg_at_k(order, {"b"}, 10) == pytest.approx(1 / math.log2(3))


def test_recall() -> None:
    order = [f"c{i:02d}" for i in range(20)]
    assert recall_at_k(order, {"c00", "c15"}, 10) == 0.5


def test_metrics_reject_empty_gold() -> None:
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], set(), 10)
