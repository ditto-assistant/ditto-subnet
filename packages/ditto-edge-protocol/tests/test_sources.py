from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator

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
    [
        (200, "public"),
        (401, "unpublished"),
        (404, "unpublished"),
        (403, "unknown"),
        (500, "unknown"),
    ],
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


class _Canned(socketserver.ThreadingTCPServer):
    """A loopback server that answers each path with fixed raw bytes."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        self.responses: dict[str, bytes] = {}
        self.hits: list[str] = []
        super().__init__(("127.0.0.1", 0), _CannedHandler)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _CannedHandler(socketserver.BaseRequestHandler):
    server: _Canned

    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        path = head.split(b" ", 2)[1].decode()
        self.server.hits.append(path)
        self.request.sendall(
            self.server.responses.get(
                path, b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
            )
        )


@pytest.fixture
def canned() -> Iterator[_Canned]:
    server = _Canned()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class _LoopbackSource(PublicShareSource):
    """The real opener and ``_get``, pointed at a loopback test server."""

    def __init__(self, base: str) -> None:
        super().__init__(share_base=SHARE, artifact_hosts=(ORIGIN,), max_bytes=1024)
        self.share_base = base
        self.base = base

    def _allowed(self, url: str) -> bool:
        return url.startswith(self.base + "/")


def test_redirects_are_never_followed(canned: _Canned) -> None:
    canned.responses["/s/moved"] = (
        b"HTTP/1.1 302 Found\r\n"
        + f"Location: {canned.base}/elsewhere\r\n".encode()
        + b"Content-Length: 0\r\n\r\n"
    )
    canned.responses["/elsewhere"] = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nsecret"
    )
    source = _LoopbackSource(canned.base)
    assert source._get(f"{canned.base}/s/moved", 1024) == (302, b"", "")
    assert source.state("moved") == "unknown"
    assert source.fetch("moved") is None
    assert "/elsewhere" not in canned.hits


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"NOT-HTTP\r\n\r\n", id="bad-status-line"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nabc",
            id="truncated-chunk",
        ),
    ],
)
def test_protocol_errors_read_as_no_answer(canned: _Canned, raw: bytes) -> None:
    canned.responses["/s/broken"] = raw
    source = _LoopbackSource(canned.base)
    assert source._get(f"{canned.base}/s/broken", 1024) == (0, b"", "")
    assert source.state("broken") == "unknown"
    assert list(source.fetch_many(["broken"])) == [("broken", None)]


def test_a_refused_connection_reads_as_no_answer(canned: _Canned) -> None:
    base = canned.base
    canned.shutdown()
    canned.server_close()
    source = _LoopbackSource(base)
    assert source.state("gone") == "unknown"
    assert source.fetch("gone") is None


def test_a_malformed_link_on_the_page_is_skipped() -> None:
    page = _page(f'<a href="https://[broken/x">x</a><img src="{CAP}">')
    source = _FakeSource(
        {
            f"{SHARE}/s/m": (200, page, "text/html"),
            CAP: (200, b"\x89PNG", "image/png"),
        }
    )
    obj = source.fetch("m")
    assert obj is not None and obj.data == b"\x89PNG"


def test_a_page_past_the_read_limit_is_never_parsed() -> None:
    from ditto_edge_protocol.limits import MIB

    # The prefix alone would parse: a complete source, then more page.
    page = _page(
        "<textarea data-copy-markdown-source>a short note</textarea>"
        + "<p>"
        + "x" * (4 * MIB)
        + "</p>"
    )
    source = _FakeSource({f"{SHARE}/s/huge": (200, page, "text/html")})
    assert source.fetch("huge") is None
