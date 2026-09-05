from __future__ import annotations

import time

import pytest

from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2PublicationReceipt,
)
from ditto.api_server.coding_private_v2_retrieval import (
    PrivateV2InputRetriever,
    PrivateV2RetrievalError,
)
from ditto.tests.api_server.test_coding_private_v2_retrieval import PLAIN, _fixture
from ditto.tests.db.queries.test_coding_hosted_private import (
    _close,
    _freeze,
    _prepared,
    _store,
)


async def _integrated(tmp_path, session_maker):
    fixture, _, reader, unwrapper = _fixture(tmp_path)
    registration = fixture._registration
    receipt = CodingPrivateV2PublicationReceipt.model_validate_json(
        (tmp_path / "receipt.json").read_bytes()
    )
    authority, _, worker, grants = await _prepared(
        session_maker, registration_bundle=(registration, receipt)
    )

    def retriever(audience):
        return PrivateV2InputRetriever(
            registration=registration,
            transport_manifest=tmp_path / "transport.json",
            payload_authority=tmp_path / "payload.json",
            publication_receipt=tmp_path / "receipt.json",
            trusted_curator_public_key_path=tmp_path / "trusted-curator-public.pem",
            reader_authority_sha256="6" * 64,
            audience=audience,
            grants=_store(session_maker, worker, audience),
            reader=reader,
            unwrapper=unwrapper,
            clock=lambda: int(time.time()),
        )

    return (
        authority,
        worker,
        grants,
        retriever("platform-authoring"),
        retriever("platform-grading"),
        reader,
        unwrapper,
    )


async def test_real_grant_store_controls_encrypted_authoring_and_grading(
    tmp_path, session_maker
):
    (
        authority,
        worker,
        grants,
        authoring,
        grading,
        reader,
        unwrapper,
    ) = await _integrated(tmp_path, session_maker)
    assert (
        await authoring.read(grant_id=grants.authoring_grant_id, role="issue") == PLAIN
    )
    for retriever, grant_id in [
        (authoring, grants.authoring_grant_id),
        (grading, grants.grading_grant_id),
    ]:
        with pytest.raises(PrivateV2RetrievalError):
            await retriever.read(grant_id=grant_id, role="grader_bundle")
    assert reader.calls == unwrapper.calls == 1
    await _freeze(session_maker, authority, worker)
    with pytest.raises(PrivateV2RetrievalError):
        await authoring.read(grant_id=grants.authoring_grant_id, role="issue")
    assert (
        await grading.read(grant_id=grants.grading_grant_id, role="grader_bundle")
        == PLAIN
    )
    await _close(session_maker, authority, worker)
    with pytest.raises(PrivateV2RetrievalError):
        await grading.read(grant_id=grants.grading_grant_id, role="grader_bundle")
    assert reader.calls == unwrapper.calls == 2


async def test_freeze_during_download_prevents_unwrap_and_plaintext_return(
    tmp_path, session_maker
):
    authority, worker, grants, authoring, _, reader, unwrapper = await _integrated(
        tmp_path, session_maker
    )
    original_get = reader.get_object

    async def read_then_freeze(*, key, max_bytes):
        ciphertext = await original_get(key=key, max_bytes=max_bytes)
        await _freeze(session_maker, authority, worker)
        return ciphertext

    reader.get_object = read_then_freeze
    with pytest.raises(PrivateV2RetrievalError) as caught:
        await authoring.read(grant_id=grants.authoring_grant_id, role="issue")
    assert reader.calls == 1 and unwrapper.calls == 0
    assert "PRIVATE_MARKER" not in str(caught.value)


async def test_close_during_unwrap_prevents_plaintext_return(tmp_path, session_maker):
    authority, worker, grants, authoring, _, reader, unwrapper = await _integrated(
        tmp_path, session_maker
    )
    original_unwrap = unwrapper.unwrap

    async def unwrap_then_close(request):
        result = await original_unwrap(request)
        await _close(session_maker, authority, worker)
        return result

    unwrapper.unwrap = unwrap_then_close
    with pytest.raises(PrivateV2RetrievalError) as caught:
        await authoring.read(grant_id=grants.authoring_grant_id, role="issue")
    assert reader.calls == unwrapper.calls == 1
    assert "PRIVATE_MARKER" not in str(caught.value)
