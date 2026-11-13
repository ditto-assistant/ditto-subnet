"""Where dry-run objects come from: synthetic bytes, or real public shares.

``PublicShareSource`` reads production the way an anonymous browser does. It
loads ``https://share.heyditto.ai/s/{id}`` with no cookie and no credential,
then takes the public bytes from the page:

* markdown, CSV and memory shares embed their raw source in the hidden
  ``data-copy-markdown-source`` textarea;
* images, PDFs, HTML artifacts and downloads link a short-lived capability URL
  on the isolated artifact origin (``a.dittousercontent.com``), which it
  fetches right away.

It only ever contacts those two hosts and never follows a redirect, and it
keeps only the bytes and a media type. Titles, usernames and the rest of the
page stay on this side, and only the share id travels on, as an operator
label. A share that is no longer public answers 401 (sign-in required) or 404,
which is how the dry run detects an unpublish.
"""

from __future__ import annotations

import hashlib
import http.client
import random
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import quote, urljoin, urlsplit

from ditto_edge_protocol.limits import MEDIA_TYPES, MIB

DEFAULT_SHARE_BASE = "https://share.heyditto.ai"
DEFAULT_ARTIFACT_HOSTS = ("a.dittousercontent.com",)

_SYNTHETIC_MIX: tuple[tuple[str, int, int, float], ...] = (
    # media type, min bytes, max bytes, share of objects
    ("text/markdown", 1_024, 20_480, 0.40),
    ("text/html", 5_120, 204_800, 0.20),
    ("image/png", 51_200, 819_200, 0.20),
    ("application/pdf", 102_400, 2 * MIB, 0.10),
    ("text/csv", 2_048, 65_536, 0.10),
)


@dataclass(frozen=True)
class SourceObject:
    data: bytes
    media_type: str
    label: str
    """Operator-side provenance (a share id, or ``synthetic:N``). Never sent to
    nodes."""


def synthetic_objects(count: int, *, seed: int = 0) -> list[SourceObject]:
    """Deterministic objects shaped like production's public-content mix."""
    rng = random.Random(seed)
    weights = [share for *_, share in _SYNTHETIC_MIX]
    objects = []
    for index in range(count):
        media_type, low, high, _ = rng.choices(_SYNTHETIC_MIX, weights=weights)[0]
        size = rng.randint(low, high)
        blocks = bytearray()
        counter = 0
        while len(blocks) < size:
            blocks.extend(hashlib.sha256(f"{seed}:{index}:{counter}".encode()).digest())
            counter += 1
        objects.append(
            SourceObject(
                data=bytes(blocks[:size]),
                media_type=media_type,
                label=f"synthetic:{index}",
            )
        )
    return objects


def _normalize_media_type(value: str | None) -> str:
    base = (value or "").split(";", 1)[0].strip().lower()
    return base if base in MEDIA_TYPES else "application/octet-stream"


class _SharePage(HTMLParser):
    """Collects the public payload of one rendered share page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.markdown: list[str] = []
        self.links: list[str] = []
        self._in_source = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = dict(attrs)
        if tag == "textarea" and "data-copy-markdown-source" in names:
            self._in_source = True
            self._buffer = []
            return
        for attribute in ("src", "data-pdf-url", "href"):
            value = names.get(attribute)
            if value and tag in ("img", "iframe", "div", "a"):
                self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._in_source:
            self.markdown.append("".join(self._buffer))
            self._in_source = False

    def handle_data(self, data: str) -> None:
        if self._in_source:
            self._buffer.append(data)


PublicState = Literal["public", "unpublished", "unknown"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so no hop can leave the https host allowlist.

    The refused redirect surfaces as its 3xx status, which reads as unknown:
    neither public nor unpublished.
    """

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class PublicShareSource:
    """Anonymous, read-only reads of public shares from the production share host."""

    def __init__(
        self,
        *,
        share_base: str = DEFAULT_SHARE_BASE,
        artifact_hosts: tuple[str, ...] = DEFAULT_ARTIFACT_HOSTS,
        max_bytes: int = 8 * MIB,
        timeout_seconds: float = 15.0,
    ) -> None:
        parts = urlsplit(share_base)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("public shares are read over https only")
        self.share_base = share_base.rstrip("/")
        self.allowed_hosts = frozenset({parts.hostname, *artifact_hosts})
        self.artifact_hosts = frozenset(artifact_hosts)
        self.max_bytes = max_bytes
        self.timeout = timeout_seconds
        # No cookie jar, no proxy, no credentials: an anonymous visitor. No
        # redirects either: _get checks the host of the only URL it fetches.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname in self.allowed_hosts

    def _get(self, url: str, limit: int) -> tuple[int, bytes, str]:
        """``(status, body, content type)``; status 0 when nothing answered."""
        if not self._allowed(url):
            return 0, b"", ""
        request = urllib.request.Request(
            url, headers={"Accept": "*/*", "User-Agent": "ditto-edge-dry-run/1"}
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data: bytes = response.read(limit + 1)
                return response.status, data, response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, b"", ""
        except (http.client.HTTPException, OSError):
            # Refused, timed out, truncated or malformed: no answer, so one bad
            # response never aborts a dry run or reads as an unpublish.
            return 0, b"", ""

    def _page_url(self, share_id: str) -> str:
        return f"{self.share_base}/s/{quote(share_id, safe='')}"

    def state(self, share_id: str) -> PublicState:
        """Whether the share is still public, as an anonymous visitor sees it."""
        status, _, _ = self._get(self._page_url(share_id), 4 * MIB)
        if status == 200:
            return "public"
        # 403 is left unknown: a WAF or bot wall can answer 403 for a share that
        # is still public, and a false unpublish would revoke a live object.
        if status in (401, 404, 410):
            return "unpublished"
        return "unknown"

    def fetch(self, share_id: str) -> SourceObject | None:
        """The share's public bytes, or ``None`` when it is not public."""
        page_url = self._page_url(share_id)
        status, body, _ = self._get(page_url, 4 * MIB)
        if status != 200 or not body:
            return None
        page = _SharePage()
        page.feed(body.decode("utf-8", "replace"))
        label = f"share:{share_id}"
        if page.markdown and page.markdown[0]:
            data = page.markdown[0].encode("utf-8")
            if len(data) <= self.max_bytes:
                return SourceObject(data, "text/markdown", label)
            return None
        for link in page.links:
            try:
                url = urljoin(page_url, link)
                host = urlsplit(url).hostname
            except ValueError:  # a malformed link on the page
                continue
            if host not in self.artifact_hosts:
                continue
            status, data, content_type = self._get(url, self.max_bytes)
            if status == 200 and data and len(data) <= self.max_bytes:
                return SourceObject(data, _normalize_media_type(content_type), label)
        return None

    def fetch_many(
        self, share_ids: list[str]
    ) -> Iterator[tuple[str, SourceObject | None]]:
        for share_id in share_ids:
            yield share_id, self.fetch(share_id)
