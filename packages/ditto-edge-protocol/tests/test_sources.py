from __future__ import annotations

import pytest

from ditto_edge_protocol.limits import MEDIA_TYPES
from ditto_edge_protocol.sources import PublicShareSource, synthetic_objects

SHARE = "https://share.example.test"
ORIGIN = "a.example-usercontent.test"
CAP = f"https://{ORIGIN}/v1/" + "t" * 43


def test_synthetic_objects_are_deterministic_and_allowed_types() -> None:
    first = synthetic_objects(25, seed=2)
    assert first == synthetic_objects(25, seed=2)
    assert first != synthetic_objects(25, seed=3)
    assert all(obj.media_type in MEDIA_TYPES for obj in first)
    assert len({obj.data for obj in first}) == 25
    assert [obj.label for obj in first[:2]] == ["synthetic:0", "synthetic:1"]


class _FakeSource(PublicShareSource):
    def __init__(self, responses: dict[str, tuple[int, bytes, str]]) -> None:
        super().__init__(share_base=SHARE, artifact_hosts=(ORIGIN,), max_bytes=1024)
        self.responses = responses
        self.requested: list[str] = []

    def _get(self, url: str, limit: int) -> tuple[int, bytes, str]:
        self.requested.append(url)
        if not url.startswith("https://") or not any(
            host in url for host in self.allowed_hosts
        ):
            return 0, b"", ""
        status, body, kind = self.responses.get(url, (404, b"", ""))
        return status, body[: limit + 1], kind


def _page(inner: str) -> bytes:
    return f"<html><body><h1>Some title by @someone</h1>{inner}</body></html>".encode()


def test_markdown_share_keeps_only_the_source_text() -> None:
    page = _page(
        "<textarea hidden data-copy-markdown-source># Hello &amp; welcome\n"
        "a *public* note</textarea><script>[data-copy-markdown-source]</script>"
    )
    source = _FakeSource({f"{SHARE}/s/s1": (200, page, "text/html")})
    obj = source.fetch("s1")
    assert obj is not None
    assert obj.data == b"# Hello & welcome\na *public* note"
    assert obj.media_type == "text/markdown"
    assert obj.label == "share:s1"
    assert b"someone" not in obj.data


def test_binary_share_follows_only_artifact_origin_links() -> None:
    page = _page(
        f'<a href="https://evil.example.test/x.png">x</a><img src="{CAP}" alt="chart">'
    )
    source = _FakeSource(
        {
            f"{SHARE}/s/s2": (200, page, "text/html"),
            CAP: (200, b"\x89PNG...", "image/png"),
        }
    )
    obj = source.fetch("s2")
    assert (
        obj is not None and obj.data == b"\x89PNG..." and obj.media_type == "image/png"
    )
    assert "https://evil.example.test/x.png" not in source.requested


def test_pdf_links_and_unknown_types_map_safely() -> None:
    page = _page(f'<div class="pdf-viewer" data-pdf-url="{CAP}"></div>')
    source = _FakeSource(
        {
            f"{SHARE}/s/p": (200, page, "text/html"),
            CAP: (200, b"%PDF-1.7", "application/x-weird"),
        }
    )
    obj = source.fetch("p")
    assert obj is not None and obj.media_type == "application/octet-stream"


def test_gated_missing_and_too_large_shares_yield_nothing() -> None:
    big = _page(f"<textarea data-copy-markdown-source>{'x' * 2000}</textarea>")
    source = _FakeSource(
        {
            f"{SHARE}/s/gated": (401, b"", "text/html"),
            f"{SHARE}/s/big": (200, big, "text/html"),
        }
    )
    assert source.fetch("gated") is None
    assert source.fetch("missing") is None
    assert source.fetch("big") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "public"), (401, "unpublished"), (404, "unpublished"), (500, "unknown")],
)
def test_state_reads_publication_like_an_anonymous_visitor(
    status: int, expected: str
) -> None:
    source = _FakeSource({f"{SHARE}/s/z": (status, b"<html></html>", "text/html")})
    assert source.state("z") == expected


def test_share_ids_are_path_escaped_and_https_is_required() -> None:
    source = _FakeSource({})
    source.fetch("../admin?x")
    assert source.requested == [f"{SHARE}/s/..%2Fadmin%3Fx"]
    with pytest.raises(ValueError):
        PublicShareSource(share_base="http://share.example.test")
