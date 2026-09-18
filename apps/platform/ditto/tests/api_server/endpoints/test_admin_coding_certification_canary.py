"""Admin contract tests for the coding-certification allowlist and lease audit."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_certification_admin import (
    CodingCertificationAllowlistEntry,
    coding_certification_allowlist_checksum,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import CodingCertificationAllowlistRevision
from ditto.db.queries.coding_certification_leases import (
    claim_coding_certification_lease,
    issue_coding_certification_lease,
)
from ditto.tests.db.queries.test_coding_certification_leases import (
    _BENCH_VERSION,
    _VALIDATOR,
    _seed_agent,
    _seed_observation,
    admit_certification_tuples,
)

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_ALLOWLIST_URL = "/api/v1/admin/coding-certification-allowlist"
_LEASES_URL = "/api/v1/admin/coding-certification-leases"
_OTHER = bittensor.Keypair.create_from_uri("//Dave").ss58_address


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _entry(agent_id: str, validator: str = _VALIDATOR) -> dict[str, str]:
    return {
        "agent_id": agent_id,
        "artifact_sha256": "ab" * 32,
        "screened_image_sha256": "cd" * 32,
        "validator_hotkey": validator,
    }


def _payload(
    *,
    expected_revision: int,
    enabled: bool,
    entries: list[dict[str, str]],
    confirmation: str | None = None,
) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "enabled": enabled,
        "entries": entries,
        "reason": "restrict certification to the team canary",
        "actor": "operator@example.com",
        "confirmation": confirmation
        or (
            f"APPLY CODING CERTIFICATION ALLOWLIST ENABLED {len(entries)}"
            if enabled
            else "APPLY CODING CERTIFICATION ALLOWLIST REFUSE ALL"
        ),
    }


async def test_allowlist_defaults_to_refuse_all_and_writes_audited_revisions(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_ALLOWLIST_URL)).status_code == 401
    assert (
        await client.post(
            _ALLOWLIST_URL,
            json=_payload(expected_revision=0, enabled=False, entries=[]),
        )
    ).status_code == 401

    initial = await client.get(_ALLOWLIST_URL, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.headers["Cache-Control"] == "no-store"
    body = initial.json()
    assert body["enabled"] is False
    assert body["effective"] == "refuse_all" and body["integrity"] == "valid"
    assert body["current"]["revision"] == 0
    assert body["current"]["effective"] == "refuse_all"
    assert body["current"]["created_at"] is None
    assert "refused for every tuple" in body["current"]["reason"]
    assert body["history"] == []
    assert body["weight_eligible"] is False

    first_agent, second_agent = str(uuid4()), str(uuid4())
    entries = [_entry(second_agent), _entry(first_agent, _OTHER)]
    wrong_phrase = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            enabled=True,
            entries=entries,
            confirmation="APPLY CODING CERTIFICATION ALLOWLIST ENABLED 1",
        ),
    )
    assert wrong_phrase.status_code == 422
    assert "APPLY CODING CERTIFICATION ALLOWLIST ENABLED 2" in wrong_phrase.text
    legacy_phrase = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            enabled=False,
            entries=[],
            confirmation="APPLY CODING CERTIFICATION ALLOWLIST DISABLED",
        ),
    )
    assert legacy_phrase.status_code == 422
    assert "APPLY CODING CERTIFICATION ALLOWLIST REFUSE ALL" in legacy_phrase.text
    for invalid in (
        # There is no open or "enabled with nothing listed" revision to write.
        _payload(expected_revision=0, enabled=True, entries=[]),
        _payload(expected_revision=0, enabled=False, entries=[_entry(first_agent)]),
        _payload(
            expected_revision=0,
            enabled=True,
            entries=[_entry(first_agent), _entry(first_agent)],
        ),
        _payload(
            expected_revision=0,
            enabled=True,
            entries=[_entry(str(uuid4())) for _ in range(17)],
        ),
        # The tuple binds the screened image: omitting it or passing anything
        # but a lowercase SHA-256 is refused, never treated as a wildcard.
        _payload(
            expected_revision=0,
            enabled=True,
            entries=[
                {
                    key: value
                    for key, value in _entry(first_agent).items()
                    if key != "screened_image_sha256"
                }
            ],
        ),
        _payload(
            expected_revision=0,
            enabled=True,
            entries=[{**_entry(first_agent), "screened_image_sha256": "*"}],
        ),
        _payload(
            expected_revision=0,
            enabled=True,
            entries=[{**_entry(first_agent), "screened_image_sha256": "CD" * 32}],
        ),
        {
            **_payload(
                expected_revision=0, enabled=True, entries=[_entry(first_agent)]
            ),
            "enabled": "true",
        },
    ):
        rejected = await client.post(_ALLOWLIST_URL, headers=_HEADERS, json=invalid)
        assert rejected.status_code == 422, invalid

    enabled = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(expected_revision=0, enabled=True, entries=entries),
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.headers["Cache-Control"] == "no-store"
    applied = enabled.json()
    assert applied["enabled"] is True
    assert applied["effective"] == "exact_tuples"
    assert applied["aborted_lease_count"] == 0
    assert applied["revoked_inference_grant_count"] == 0
    current = applied["current"]
    assert current["revision"] == 1 and current["parent_revision"] == 0
    assert current["actor"] == "operator@example.com"
    assert current["reason"] == "restrict certification to the team canary"
    canonical = sorted(
        entries,
        key=lambda item: (
            item["agent_id"],
            item["artifact_sha256"],
            item["screened_image_sha256"],
            item["validator_hotkey"],
        ),
    )
    assert current["entries"] == canonical
    assert current["checksum"] == coding_certification_allowlist_checksum(
        enabled=True,
        entries=[CodingCertificationAllowlistEntry.model_validate(e) for e in entries],
    )

    stale = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(expected_revision=0, enabled=False, entries=[]),
    )
    assert stale.status_code == 409
    assert "expected_revision=1" in stale.text

    disabled = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(expected_revision=1, enabled=False, entries=[]),
    )
    assert disabled.status_code == 200, disabled.text
    read = await client.get(f"{_ALLOWLIST_URL}?history_limit=1", headers=_HEADERS)
    assert read.json()["enabled"] is False
    assert read.json()["effective"] == "refuse_all"
    assert read.json()["current"]["revision"] == 2
    assert [item["revision"] for item in read.json()["history"]] == [2]
    full = await client.get(_ALLOWLIST_URL, headers=_HEADERS)
    assert [item["revision"] for item in full.json()["history"]] == [2, 1]
    assert full.json()["history"][1]["entries"] == canonical


async def test_lease_audit_is_admin_only_paginated_newest_first_and_redacted(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_LEASES_URL)).status_code == 401
    lease_ids = []
    async with session_maker() as session:
        for index in range(3):
            agent = await _seed_agent(session)
            await _seed_observation(session, agent, evidence_sha256=f"{index}1" * 32)
            await admit_certification_tuples(
                session,
                (
                    agent.agent_id,
                    agent.sha256,
                    agent.screened_image_sha256 or "",
                    _VALIDATOR,
                ),
            )
            async with session.begin():
                issued = await issue_coding_certification_lease(
                    session,
                    validator_hotkey=_VALIDATOR,
                    agent_id=agent.agent_id,
                    bench_version=_BENCH_VERSION,
                )
            lease_ids.append(issued.row.lease_id)
        async with session.begin():
            await claim_coding_certification_lease(
                session, validator_hotkey=_VALIDATOR, lease_id=lease_ids[1]
            )

    page = await client.get(f"{_LEASES_URL}?limit=2&offset=0", headers=_HEADERS)
    assert page.status_code == 200, page.text
    assert page.headers["Cache-Control"] == "no-store"
    body = page.json()
    assert body["total"] == 3 and body["limit"] == 2 and body["offset"] == 0
    assert [row["lease_id"] for row in body["leases"]] == [
        str(lease_ids[2]),
        str(lease_ids[1]),
    ]
    claimed = body["leases"][1]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"] is not None
    assert claimed["deadline_passed"] is False
    assert claimed["claim_allowlist_revision"] == 3
    assert claimed["aborted_allowlist_revision"] is None
    assert claimed["receipt_window_ends_at"] > claimed["deadline"]
    assert claimed["inference_grant_status"] is None
    assert claimed["receipt_status"] is None
    assert claimed["weight_eligible"] is False
    for forbidden in (
        "grant_id",
        "bearer",
        "broker_public_key",
        "screened_image_ref",
        "screened_image_upload_id",
        "authority",
    ):
        assert forbidden not in page.text

    rest = await client.get(f"{_LEASES_URL}?limit=2&offset=2", headers=_HEADERS)
    assert [row["lease_id"] for row in rest.json()["leases"]] == [str(lease_ids[0])]
    filtered = await client.get(f"{_LEASES_URL}?status=claimed", headers=_HEADERS)
    assert filtered.json()["total"] == 1
    assert filtered.json()["leases"][0]["lease_id"] == str(lease_ids[1])
    assert (
        await client.get(f"{_LEASES_URL}?validator_hotkey={_OTHER}", headers=_HEADERS)
    ).json()["total"] == 0
    assert (
        await client.get(f"{_LEASES_URL}?limit=201", headers=_HEADERS)
    ).status_code == 422

    # Refusing everything aborts every in-flight lease and names the revision.
    refuse_all = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(expected_revision=3, enabled=False, entries=[]),
    )
    assert refuse_all.status_code == 200, refuse_all.text
    assert refuse_all.json()["aborted_lease_count"] == 3
    aborted = await client.get(f"{_LEASES_URL}?status=aborted", headers=_HEADERS)
    assert aborted.json()["total"] == 3
    assert {row["aborted_allowlist_revision"] for row in aborted.json()["leases"]} == {
        4
    }
    claimed_then_aborted = next(
        row for row in aborted.json()["leases"] if row["lease_id"] == str(lease_ids[1])
    )
    assert claimed_then_aborted["claimed_at"] is not None


async def test_corrupt_allowlist_revision_reads_as_invalid_and_refuse_all(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id = str(uuid4())
    written = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(expected_revision=0, enabled=True, entries=[_entry(agent_id)]),
    )
    assert written.status_code == 200, written.text
    async with session_maker() as session, session.begin():
        latest = await session.scalar(
            select(func.max(CodingCertificationAllowlistRevision.revision))
        )
        session.add(
            CodingCertificationAllowlistRevision(
                parent_revision=int(latest or 0),
                enabled=True,
                entries=[_entry(agent_id), _entry(str(uuid4()))],
                checksum=coding_certification_allowlist_checksum(
                    enabled=True,
                    entries=[
                        CodingCertificationAllowlistEntry.model_validate(
                            _entry(agent_id)
                        )
                    ],
                ),
                reason="tampered revision fixture",
                actor="test",
            )
        )
    read = await client.get(_ALLOWLIST_URL, headers=_HEADERS)
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["enabled"] is False
    assert body["integrity"] == "invalid" and body["effective"] == "refuse_all"
    assert body["current"]["revision"] == 2 and body["current"]["enabled"] is True
    assert body["current"]["integrity"] == "invalid"
    assert body["current"]["entries"] == []
    assert [item["integrity"] for item in body["history"]] == ["invalid", "valid"]
    # An operator repairs it by appending a new intact revision.
    repaired = await client.post(
        _ALLOWLIST_URL,
        headers=_HEADERS,
        json=_payload(expected_revision=2, enabled=True, entries=[_entry(agent_id)]),
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["integrity"] == "valid"
    assert repaired.json()["enabled"] is True
