"""Private transport remains bounded, signed and byte-exact."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import bittensor
import httpx
import pytest

from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.validator.dittobench import DittobenchClient, DittobenchError
from ditto.validator.platform import (
    PlatformClient,
    PlatformError,
    PlatformInfrastructureError,
)


async def test_private_download_is_signed_and_checks_bytes():
    key = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = uuid4()
    artifact = b'{"private":"fixture"}\n'
    digest = hashlib.sha256(artifact).hexdigest()
    deadline = datetime.now(UTC) + timedelta(hours=1)
    seen = []

    def handler(request):
        payload = PrivateDatasetRequest.model_validate_json(request.content)
        assert key.verify(
            payload.signing_message(agent_id), bytes.fromhex(payload.signature)
        )
        assert payload.dataset_sha256 == digest
        assert payload.deadline == deadline
        seen.append(payload.nonce)
        return httpx.Response(200, content=artifact)

    config = SimpleNamespace(
        platform_api_url="https://platform.test", validator_hotkey=key.ss58_address
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(config, http, key)
        for _ in range(2):
            assert (
                await client.get_private_dataset(
                    agent_id, dataset_sha256=digest, deadline=deadline
                )
                == artifact
            )
    assert len(set(seen)) == 2


@pytest.mark.parametrize(
    "status,body", [(403, b"private error"), (200, b"wrong"), (200, b"")]
)
async def test_private_download_never_falls_back(status, body):
    key = bittensor.Keypair.create_from_uri("//Alice")
    config = SimpleNamespace(
        platform_api_url="https://platform.test", validator_hotkey=key.ss58_address
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))
    ) as http:
        client = PlatformClient(config, http, key)
        with pytest.raises(PlatformError) as error:
            await client.get_private_dataset(
                uuid4(), dataset_sha256="a" * 64, deadline=datetime.now(UTC)
            )
        assert "private error" not in str(error.value)


async def test_private_scorer_transport_and_missing_mode():
    artifact = b'{"private":"fixture"}\n'
    digest = hashlib.sha256(artifact).hexdigest()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "private-run"})

    config = SimpleNamespace(dittobench_api_url="http://scorer.test", run_size="full")
    kwargs = {
        "tarball_url": "https://source.test/agent.tgz",
        "bench_version": 13,
        "dataset_sha256": digest,
        "private_dataset_bytes": artifact,
        "screened_image_url": "https://source.test/image.tar",
        "screened_image_sha256": "a" * 64,
        "screened_image_size_bytes": 123,
        "screened_image_id": "sha256:" + "b" * 64,
        "screened_image_ref": "fixture",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)
        with pytest.raises(DittobenchError):
            await client._submit(**kwargs)
        assert not requests
        assert (
            await client._submit(**kwargs, private_dataset_mode="platform-private-v1")
            == "private-run"
        )
    assert base64.b64decode(requests[0]["private_dataset_bytes"]) == artifact
    assert requests[0]["dataset_sha256"] == digest
    assert "env" not in requests[0]


@pytest.mark.parametrize(
    "mode,bench_version,accepted",
    [
        ("generator-round-v1", 13, True),
        ("generator-round-v1", 14, True),
        ("generator-round-v1", 12, False),
        ("platform-private-v1", 13, True),
        ("platform-private-v1", 14, False),
    ],
)
async def test_private_modes_accept_their_own_bench_versions(
    mode, bench_version, accepted
):
    # Round 25988 (the first after v14 activated) voided: every validator
    # refused the v14 round dataset before reaching the scorer.
    artifact = b'{"round":"fixture"}\n'
    digest = hashlib.sha256(artifact).hexdigest()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "round-run"})

    config = SimpleNamespace(dittobench_api_url="http://scorer.test", run_size="full")
    kwargs = {
        "tarball_url": "https://source.test/agent.tgz",
        "bench_version": bench_version,
        "dataset_sha256": digest,
        "private_dataset_bytes": artifact,
        "private_dataset_mode": mode,
        "screened_image_url": "https://source.test/image.tar",
        "screened_image_sha256": "a" * 64,
        "screened_image_size_bytes": 123,
        "screened_image_id": "sha256:" + "b" * 64,
        "screened_image_ref": "fixture",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)
        if accepted:
            assert await client._submit(**kwargs) == "round-run"
            assert requests[0]["bench_version"] == bench_version
            assert requests[0]["private_dataset_mode"] == mode
        else:
            with pytest.raises(DittobenchError):
                await client._submit(**kwargs)
            assert not requests


def _private_client(http):
    key = bittensor.Keypair.create_from_uri("//Alice")
    config = SimpleNamespace(
        platform_api_url="https://platform.test", validator_hotkey=key.ss58_address
    )
    return PlatformClient(config, http, key)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_private_download_status_is_platform_infrastructure(status):
    # ditto-subnet#2967: a Platform blip is not the agent's fault, so it must not
    # reach the worker as a plain PlatformError (handed back as scoring_error).
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=b"private error")
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError) as error:
            await _private_client(http).get_private_dataset(
                uuid4(), dataset_sha256="a" * 64, deadline=datetime.now(UTC)
            )
    assert str(status) in str(error.value)
    assert "private error" not in str(error.value)


@pytest.mark.parametrize(
    "transport_error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
)
async def test_private_download_transport_failure_is_platform_infrastructure(
    transport_error,
):
    def handler(_):
        raise transport_error

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError):
            await _private_client(http).get_private_dataset(
                uuid4(), dataset_sha256="a" * 64, deadline=datetime.now(UTC)
            )


@pytest.mark.parametrize("status", [403, 409])
async def test_definite_private_download_rejection_stays_a_platform_error(status):
    # A rejected lease or a missing dataset will not heal by retrying, and a
    # no-fault hand-back would park it for an operator instead of resolving it.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=b"private error")
        )
    ) as http:
        with pytest.raises(PlatformError) as error:
            await _private_client(http).get_private_dataset(
                uuid4(), dataset_sha256="a" * 64, deadline=datetime.now(UTC)
            )
    assert not isinstance(error.value, PlatformInfrastructureError)
    assert str(status) in str(error.value)
    assert "private error" not in str(error.value)
