"""Owner download byte/attachment proof in the object-storage CI suite."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import httpx
import pytest

from ditto.api_server import create_api_server
from ditto.api_server.dependencies import get_storage_client
from ditto.api_server.storage import (
    create_storage_client,
    parse_storage_config_from_env,
)
from ditto.db.models import Agent
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.endpoints.test_miner_artifact import seed_access


@pytest.mark.integration
async def test_owner_link_downloads_original_bytes_as_attachment(
    session_maker, object_storage
):
    del object_storage
    app = create_api_server(make_api_server_config())
    app.state.storage = MagicMock()
    agent_id, headers, _, _ = await seed_access(app, session_maker)
    original = b"original submitted source archive"
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        agent.sha256 = hashlib.sha256(original).hexdigest()
    async with create_storage_client(parse_storage_config_from_env()) as storage:
        await storage.put_object(key=f"{agent_id}/agent.tar.gz", body=original)

        async def get_storage():
            return storage

        app.dependency_overrides[get_storage_client] = get_storage
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            got = await client.get(
                f"/api/v1/me/agents/{agent_id}/artifact", headers=headers
            )
        assert got.status_code == 200, got.text
        async with httpx.AsyncClient() as downloader:
            archive = await downloader.get(got.json()["download_url"])
        assert archive.status_code == 200
        assert archive.content == original
        assert (
            f"ditto-agent-{agent_id}-v3.tar.gz"
            in archive.headers["content-disposition"]
        )
        assert archive.headers["content-disposition"].startswith("attachment;")
        # The API digest describes the uploaded bytes, not a screened image.
        assert hashlib.sha256(archive.content).hexdigest() == got.json()["sha256"]
