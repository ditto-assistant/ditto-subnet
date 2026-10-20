"""Database-free coverage for the any-pair lineage source diff (#2673).

The HTTP round trips, audit rows, and copy-review parity live with the other
source-diff tests in ``test_admin_copy_review.py`` (they need Postgres). These
pin the route contract and the pair loader without a database.
"""

import gzip
import hashlib
import io
import tarfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ditto.api_server import create_api_server
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints import admin_copy_review
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.db.models import Agent, AgentStatus, ArtifactFetchAudit
from ditto.db.queries import artifact_fetch_audit
from ditto.tests.api_server.conftest import make_api_server_config

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_LINEAGE = "/api/v1/admin/agents/{agent_id}/lineage-source-diff"
_COPY_REVIEW = "/api/v1/admin/copy-reviews/{agent_id}/source-diff"


def _tarball(files: dict[str, str]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for path, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def _agent(agent_id: UUID, tar: bytes) -> Agent:
    return Agent(
        agent_id=agent_id,
        miner_hotkey="5Lineage",
        name=f"agent-{agent_id.hex[:6]}",
        sha256=hashlib.sha256(tar).hexdigest(),
        status=AgentStatus.SCORED,
    )


def _fakes(
    agents: dict[UUID, Agent], objects: dict[str, bytes]
) -> tuple[MagicMock, MagicMock]:
    session = MagicMock()

    async def _get(model: type, key: UUID) -> Agent | None:
        assert model is Agent
        return agents.get(key)

    session.get = AsyncMock(side_effect=_get)
    storage = MagicMock()

    async def _get_object(*, key: str, max_bytes: int) -> bytes:
        del max_bytes
        if key not in objects:
            raise ObjectDownloadFailedError(key)
        return objects[key]

    storage.get_object = AsyncMock(side_effect=_get_object)
    return session, storage


def _pair_fixture(
    candidate_files: dict[str, str], reference_files: dict[str, str]
) -> tuple[UUID, UUID, MagicMock, MagicMock]:
    candidate_id, reference_id = uuid4(), uuid4()
    candidate_tar, reference_tar = _tarball(candidate_files), _tarball(reference_files)
    session, storage = _fakes(
        {
            candidate_id: _agent(candidate_id, candidate_tar),
            reference_id: _agent(reference_id, reference_tar),
        },
        {
            f"{candidate_id}/agent.tar.gz": candidate_tar,
            f"{reference_id}/agent.tar.gz": reference_tar,
        },
    )
    return candidate_id, reference_id, session, storage


def _operation(schema: dict[str, Any], template: str, suffix: str = "") -> Any:
    return schema["paths"][template + suffix]["get"]


def test_lineage_routes_share_the_copy_review_wire_shapes() -> None:
    schema = create_api_server(make_api_server_config()).openapi()
    for suffix in ("", "/file"):
        lineage = _operation(schema, _LINEAGE, suffix)
        copy_review = _operation(schema, _COPY_REVIEW, suffix)
        assert (
            lineage["responses"]["200"]["content"]
            == copy_review["responses"]["200"]["content"]
        )
        params = {param["name"]: param for param in lineage["parameters"]}
        assert params["agent_id"]["in"] == "path"
        reference = params["reference_agent_id"]
        assert reference["in"] == "query" and reference["required"] is True
        assert reference["schema"]["format"] == "uuid"
        assert params["x-admin-actor"]["in"] == "header"
    file_params = {
        param["name"]: param
        for param in _operation(schema, _LINEAGE, "/file")["parameters"]
    }
    assert file_params["path"]["required"] is True
    assert file_params["path"]["schema"]["maxLength"] == 240


def test_lineage_audit_endpoints_are_their_own_doors() -> None:
    names = {
        artifact_fetch_audit.ENDPOINT_ADMIN_COPY_REVIEW_DIFF,
        artifact_fetch_audit.ENDPOINT_ADMIN_COPY_REVIEW_DIFF_FILE,
        artifact_fetch_audit.ENDPOINT_ADMIN_LINEAGE_DIFF,
        artifact_fetch_audit.ENDPOINT_ADMIN_LINEAGE_DIFF_FILE,
    }
    assert len(names) == 4
    assert artifact_fetch_audit.ENDPOINT_ADMIN_LINEAGE_DIFF == (
        "admin.get_lineage_source_diff"
    )
    assert artifact_fetch_audit.ENDPOINT_ADMIN_LINEAGE_DIFF_FILE == (
        "admin.get_lineage_source_diff_file"
    )


async def test_same_agent_on_both_sides_is_400_before_any_read() -> None:
    agent_id = uuid4()
    session, storage = _fakes({}, {})
    with pytest.raises(HTTPException) as caught:
        await admin_copy_review._lineage_diff_pair(agent_id, agent_id, session, storage)
    assert caught.value.status_code == 400
    session.get.assert_not_called()
    storage.get_object.assert_not_called()


async def test_missing_agent_or_reference_is_404() -> None:
    candidate_id, reference_id, session, storage = _pair_fixture(
        {"a.rs": "x\n"}, {"a.rs": "y\n"}
    )
    for agent_id, other_id, detail in (
        (uuid4(), reference_id, "agent not found"),
        (candidate_id, uuid4(), "reference agent not found"),
    ):
        with pytest.raises(HTTPException) as caught:
            await admin_copy_review._lineage_diff_pair(
                agent_id, other_id, session, storage
            )
        assert (caught.value.status_code, caught.value.detail) == (404, detail)
    storage.get_object.assert_not_called()


async def test_missing_or_tampered_artifact_is_502() -> None:
    candidate_id, reference_id, session, storage = _pair_fixture(
        {"a.rs": "x\n"}, {"a.rs": "y\n"}
    )
    real_get = storage.get_object.side_effect

    async def _drop_reference(*, key: str, max_bytes: int) -> bytes:
        if key.startswith(str(reference_id)):
            raise ObjectDownloadFailedError(key)
        return await real_get(key=key, max_bytes=max_bytes)

    storage.get_object = AsyncMock(side_effect=_drop_reference)
    with pytest.raises(HTTPException) as caught:
        await admin_copy_review._lineage_diff_pair(
            candidate_id, reference_id, session, storage
        )
    assert caught.value.status_code == 502

    async def _tamper(*, key: str, max_bytes: int) -> bytes:
        del key, max_bytes
        return _tarball({"a.rs": "tampered\n"})

    storage.get_object = AsyncMock(side_effect=_tamper)
    with pytest.raises(HTTPException) as caught:
        await admin_copy_review._lineage_diff_pair(
            candidate_id, reference_id, session, storage
        )
    assert caught.value.status_code == 502


async def test_manifest_classifies_reference_to_candidate_with_renames() -> None:
    candidate_id, reference_id, session, storage = _pair_fixture(
        {
            "src/main.rs": "fn main() {}\n",
            "src/util.rs": "fn util() -> i32 { 1 }\n",
            "src/renamed.rs": "fn kept() -> i32 {\n    41 + 1\n}\n",
            "src/new.rs": "fn fresh() {}\n",
        },
        {
            "src/main.rs": "fn main() {}\n",
            "src/util.rs": "fn util() -> i32 { 2 }\n",
            "src/original.rs": "fn kept() -> i32 {\n    41 + 1\n}\n",
            "src/mechanism.rs": "fn rejected_mechanism() { panic!() }\n",
        },
    )
    pair = await admin_copy_review._lineage_diff_pair(
        candidate_id, reference_id, session, storage
    )
    manifest = await admin_copy_review._source_diff_manifest(pair)

    assert manifest.agent_id == candidate_id
    assert manifest.reference_agent_id == reference_id
    assert manifest.candidate_sha256 == pair.candidate.sha256
    assert manifest.reference_sha256 == pair.reference.sha256
    by_path = {entry.path: entry for entry in manifest.files}
    assert by_path["src/main.rs"].status == "identical"
    assert by_path["src/util.rs"].status == "modified"
    assert by_path["src/new.rs"].status == "added"
    assert by_path["src/mechanism.rs"].status == "removed"
    assert by_path["src/renamed.rs"].status == "renamed"
    assert by_path["src/renamed.rs"].from_path == "src/original.rs"
    assert manifest.omitted_paths == []


async def test_file_detail_diffs_one_path_and_404s_on_a_ghost() -> None:
    candidate_id, reference_id, session, storage = _pair_fixture(
        {"src/util.rs": "fn util() -> i32 { 1 }\n"},
        {"src/util.rs": "fn util() -> i32 { 2 }\n"},
    )
    pair = await admin_copy_review._lineage_diff_pair(
        candidate_id, reference_id, session, storage
    )
    detail = await admin_copy_review._source_diff_file_detail(pair, "src/util.rs")
    assert (detail.agent_id, detail.reference_agent_id) == (candidate_id, reference_id)
    assert detail.candidate_present and detail.reference_present
    assert not detail.identical
    joined = "\n".join(detail.diff_lines)
    assert "{ 2 }" in joined and "{ 1 }" in joined

    with pytest.raises(HTTPException) as caught:
        await admin_copy_review._source_diff_file_detail(pair, "ghost.rs")
    assert caught.value.status_code == 404


async def test_audit_writes_one_row_per_exposed_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id, reference_id, session, storage = _pair_fixture(
        {"a.rs": "x\n"}, {"a.rs": "y\n"}
    )
    pair = await admin_copy_review._lineage_diff_pair(
        candidate_id, reference_id, session, storage
    )
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_copy_review, "record_artifact_fetch", record)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("10.0.0.7", 1234),
        }
    )

    await admin_copy_review._audit_diff_pair(
        session,
        request=request,
        actor="operator",
        endpoint=artifact_fetch_audit.ENDPOINT_ADMIN_LINEAGE_DIFF_FILE,
        candidate=pair.candidate,
        reference=pair.reference,
        path="a.rs",
    )

    calls = [call.kwargs for call in record.await_args_list]
    assert [(c["agent_id"], c["detail"]["role"]) for c in calls] == [
        (candidate_id, "candidate"),
        (reference_id, "reference"),
    ]
    for call in calls:
        assert call["endpoint"] == "admin.get_lineage_source_diff_file"
        assert call["requester_kind"] == "admin"
        assert call["requester_id"] == "operator"
        assert call["source_ip"] == "10.0.0.7"
        assert call["detail"]["path"] == "a.rs"


async def test_http_round_trip_serves_and_audits_without_a_review_row() -> None:
    """Drive the real routes over ASGI with a fake session and storage.

    Pins routing, auth, query binding, and the two audit rows end to end where
    no Postgres is available; the DB-backed twins in test_admin_copy_review.py
    cover the same flow against real tables.
    """
    candidate_id, reference_id, session, storage = _pair_fixture(
        {"src/util.rs": "fn util() -> i32 { 1 }\n"},
        {"src/util.rs": "fn util() -> i32 { 2 }\n"},
    )
    added: list[ArtifactFetchAudit] = []
    session.add = MagicMock(side_effect=added.append)
    session.commit = AsyncMock()
    app = create_api_server(make_api_server_config(admin_api_token=_TOKEN))

    async def _session() -> Any:
        yield session

    async def _storage() -> MagicMock:
        return storage

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_storage_client] = _storage
    base = _LINEAGE.format(agent_id=candidate_id)
    params = {"reference_agent_id": str(reference_id)}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        manifest = await client.get(base, params=params, headers=_HEADERS)
        detail = await client.get(
            f"{base}/file", params={**params, "path": "src/util.rs"}, headers=_HEADERS
        )
        same = await client.get(
            base, params={"reference_agent_id": str(candidate_id)}, headers=_HEADERS
        )
        unauthenticated = await client.get(
            base, params=params, headers={"X-Admin-Actor": "operator"}
        )

    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["files"][0]["status"] == "modified"
    assert detail.status_code == 200, detail.text
    assert detail.json()["reference_agent_id"] == str(reference_id)
    assert same.status_code == 400
    assert unauthenticated.status_code == 401
    assert [(row.endpoint, row.agent_id) for row in added] == [
        ("admin.get_lineage_source_diff", candidate_id),
        ("admin.get_lineage_source_diff", reference_id),
        ("admin.get_lineage_source_diff_file", candidate_id),
        ("admin.get_lineage_source_diff_file", reference_id),
    ]
