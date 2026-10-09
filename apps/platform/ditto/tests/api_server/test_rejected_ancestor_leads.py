"""Exact source binding, citation bounds, and whitespace-only normalization."""

import asyncio
import hashlib
import io
import tarfile
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_server import rejected_ancestor_leads as leads
from ditto.api_server.rejected_ancestor_leads import artifact_windows
from ditto.api_server.storage import ObjectNotFoundError
from ditto.db.queries.rejected_ancestors import RejectedAncestor
from ditto_screening_protocol import ArtifactResponse
from ditto_screening_protocol.rejected_ancestor import matching_windows, source_tokens

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.storage import S3StorageClient
    from ditto.db.models import Agent

_SOURCE = (
    "fn serve() {\n let limit = 3;\n"
    ' if calls > limit { return "rejected"; }\n dispatch();\n}\n'
)


def archive(source: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        data = source.encode()
        member = tarfile.TarInfo("src/old.rs")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    return stream.getvalue()


def ancestor(data: bytes, *references: str) -> RejectedAncestor:
    return RejectedAncestor(
        uuid4(), hashlib.sha256(data).hexdigest(), references, datetime.now(UTC)
    )


def test_matches_moved_reformatted_code_but_never_changed_literal_or_mechanism() -> (
    None
):
    data = archive(_SOURCE)
    windows = artifact_windows(ancestor(data, "src/old.rs:3"), data)
    assert len(windows) == 1
    moved = (
        "\n\nfn serve ( ) { let limit=3; "
        'if calls>limit {return "rejected";} dispatch ( ); }'
    )
    assert matching_windows(moved, windows) == [(3, windows[0])]
    assert matching_windows(moved.replace('"rejected"', '"remediated"'), windows) == []
    assert matching_windows(moved.replace("limit=3", "limit=30"), windows) == []
    # The fast rolling filter cannot fabricate a match even when its value is
    # copied into a different fingerprint.
    corrupt = windows[0].model_copy(update={"sha256": "0" * 64})
    assert matching_windows(moved, [corrupt]) == []
    assert matching_windows(moved, windows, limit=0) == []


def test_preserves_multiline_literal_contents_and_line_numbers() -> None:
    assert source_tokens('x = "one\ntwo";\n next();') == [
        ("x", 1),
        ("=", 1),
        ('"one\ntwo"', 1),
        (";", 2),
        ("next", 3),
        ("(", 3),
        (")", 3),
        (";", 3),
    ]


def test_long_spans_windowed_without_spending_budget_on_duplicates() -> None:
    source = "\n".join(
        f'fn entry_{n}() {{ dispatch("label-{n}"); }}' for n in range(200)
    )
    data = archive(source)
    windows = artifact_windows(ancestor(data, "`src/old.rs:L1-L200`"), data)
    assert len(windows) == 32
    assert all(window.end_line - window.start_line == 4 for window in windows)
    assert matching_windows("\n".join(source.splitlines()[35:40]), windows) == [
        (1, windows[7])
    ]
    repeated = artifact_windows(ancestor(data, *(["src/old.rs:3"] * 64)), data)
    assert len(repeated) == 1


def test_ignores_missing_unsafe_and_stale_citations_and_bounds_the_payload() -> None:
    data = archive(_SOURCE)
    bad = [
        "../secret:1",
        "/etc/passwd:1",
        "src/old.rs:0",
        "src/old.rs:99",
        "src/old.rs:4-2",
        "src/old.rs:1-50",
        "a comment, not a source citation",
    ]
    assert artifact_windows(ancestor(data, *bad), data) == []
    assert len(artifact_windows(ancestor(data, *(["src/old.rs:3"] * 64)), data)) <= 32
    with pytest.raises(ValueError, match="digest mismatch"):
        artifact_windows(
            ancestor(data, "src/old.rs:3"), archive(_SOURCE + "\nchanged\n")
        )


def test_legacy_artifact_metadata_and_future_fields_remain_compatible() -> None:
    data = archive(_SOURCE)
    old = {
        "agent_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "download_url": "https://storage.invalid/agent",
        "expires_at": datetime.now(UTC),
        "future_field": True,
    }
    assert ArtifactResponse.model_validate(old).rejected_ancestor_windows == []
    window = artifact_windows(ancestor(data, "src/old.rs:3"), data)[0]
    current = {**old, "rejected_ancestor_windows": [window.model_dump()]}
    assert ArtifactResponse.model_validate(current).rejected_ancestor_windows == [
        window
    ]
    with pytest.raises(ValidationError):
        ArtifactResponse.model_validate(
            {
                **old,
                "rejected_ancestor_windows": [
                    {**window.model_dump(), "token_count": 257}
                ],
            }
        )


@pytest.mark.parametrize("missing", [True, False])
async def test_unavailable_or_corrupt_old_source_is_visible_without_blocking_fetch(
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    data = archive(_SOURCE)
    old = ancestor(data, "src/old.rs:3")
    monkeypatch.setattr(leads, "rejected_ancestors", AsyncMock(return_value=[old]))
    reader = (
        AsyncMock(side_effect=ObjectNotFoundError("old object was purged"))
        if missing
        else AsyncMock(return_value=b"wrong old artifact")
    )
    windows, unavailable = await leads.rejected_ancestor_windows(
        cast("AsyncSession", None),
        cast("S3StorageClient", SimpleNamespace(get_object=reader)),
        candidate=cast("Agent", None),
    )
    assert windows == []
    assert unavailable == [old.agent_id]


async def test_stalled_historical_download_is_cancelled_without_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = ancestor(archive(_SOURCE), "src/old.rs:3")
    cancelled = asyncio.Event()

    async def stalled(**_kwargs: object) -> bytes:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return b""

    monkeypatch.setattr(leads, "rejected_ancestors", AsyncMock(return_value=[old]))
    monkeypatch.setattr(leads, "_ANCESTOR_SOURCE_TIMEOUT_SECONDS", 0.01)
    windows, unavailable = await leads.rejected_ancestor_windows(
        cast("AsyncSession", None),
        cast("S3StorageClient", SimpleNamespace(get_object=stalled)),
        candidate=cast("Agent", None),
    )
    assert windows == []
    assert unavailable == [old.agent_id]
    assert cancelled.is_set()
