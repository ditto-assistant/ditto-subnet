"""Build-bounty publication and the public board (issue #2942, P1)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.bounty import BountyTerms, bounty_spec_digest
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.db.models import Bounty, BountyEvent, BountyRevision, BountyRound, BountySlot
from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
    _HEADERS as _ADMIN_TOKEN,
)
from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
    _install,
)

pytestmark = pytest.mark.asyncio
_ACTOR = "operator-fixture@example.com"
_REVIEWER = "reviewer-private@example.com"
_HEADERS = {**_ADMIN_TOKEN, "X-Admin-Actor": _ACTOR}
_REPO = "ditto-assistant/ditto-subnet"
TERMS = {
    "title": "TEST FIXTURE bounty",
    "scope": "Fixture scope. Not real work.",
    "public_interfaces": "Fixture interface.",
    "acceptance_evidence": "Fixture tests pass.",
    "dependencies": ["ditto-assistant/ditto-subnet#2889"],
    "reviewer_login": "reviewer-fixture",
    "entry_fee_alpha_rao": 2_000_000_000,
    "reward_pool_alpha_rao": 100_000_000_000,
    "funding_mode": "rehearsal",
    "fee_escrow_coldkey": "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "fee_escrow_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
}


def _request(*, issue=2942, expected=0, terms=None, **extra):
    return {
        "repo": _REPO,
        "issue": issue,
        "expected_revision": expected,
        "terms": {**TERMS, **(terms or {})},
        "reviewer_actor": _REVIEWER,
        "reason": "publish a test-only fixture bounty",
        **extra,
    }


async def preview(client, **kwargs):
    body = _request(**kwargs)
    for key in ("reviewer_actor", "reason"):
        body.pop(key)
    response = await client.post(
        "/api/v1/admin/bounties/preview", headers=_HEADERS, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def publish(client, **kwargs):
    phrase = (await preview(client, **kwargs))["confirmation"]
    return await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_HEADERS,
        json=_request(**kwargs, confirmation=phrase),
    )


async def counts(maker) -> dict[str, int]:
    async with maker() as session:
        return {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model))
            )
            for model in (Bounty, BountyRevision, BountyRound, BountySlot, BountyEvent)
        }


async def test_preview_returns_the_exact_digest_and_phrase_and_writes_nothing(
    app, client, session_maker
):
    _install(app, session_maker)
    shown = await preview(client)
    digest = bounty_spec_digest(
        repo=_REPO, issue=2942, revision=1, terms=BountyTerms.model_validate(TERMS)
    )
    assert shown == {
        "revision": 1,
        "spec_digest": digest,
        "confirmation": f"PUBLISH BOUNTY {_REPO}#2942 REVISION 1 {digest}",
        "refusals": [],
    }
    assert set((await counts(session_maker)).values()) == {0}


async def test_first_revision_opens_round_one_with_three_open_slots(
    app, client, session_maker
):
    _install(app, session_maker)
    published = await publish(client)
    assert published.status_code == 200, published.text
    assert published.json()["revision"] == 1
    board = (await client.get("/api/v1/public/bounties")).json()
    assert board["total"] == 1
    summary = board["items"][0]
    assert summary["slots"] == {
        "open": 3,
        "held": 0,
        "claimed": 0,
        "submitted": 0,
        "rework": 0,
        "closed": 0,
    }
    assert (summary["current_round"], summary["current_revision"]) == (1, 1)
    detail = (
        await client.get(f"/api/v1/public/bounties/{summary['bounty_id']}")
    ).json()
    assert [row["revision"] for row in detail["revisions"]] == [1]
    assert detail["revisions"][0]["terms"] == BountyTerms.model_validate(
        TERMS
    ).model_dump(mode="json")
    assert [(r["round"], r["opened_revision"]) for r in detail["rounds"]] == [(1, 1)]
    assert [(s["round"], s["position"], s["status"]) for s in detail["slots"]] == [
        (1, 1, "open"),
        (1, 2, "open"),
        (1, 3, "open"),
    ]
    assert [e["kind"] for e in detail["events"]] == [
        "revision_published",
        "round_opened",
    ]


async def test_later_revision_keeps_existing_slots_and_round(
    app, client, session_maker
):
    _install(app, session_maker)
    first = await publish(client)
    assert first.status_code == 200, first.text
    bounty_id = (await client.get("/api/v1/public/bounties")).json()["items"][0][
        "bounty_id"
    ]
    before = (await client.get(f"/api/v1/public/bounties/{bounty_id}")).json()
    second = await publish(
        client, expected=1, terms={"entry_fee_alpha_rao": 3_000_000_000}
    )
    assert second.status_code == 200, second.text
    after = (await client.get(f"/api/v1/public/bounties/{bounty_id}")).json()
    assert [row["revision"] for row in after["revisions"]] == [1, 2]
    # New terms never rewrite rounds or slots that already exist.
    assert after["rounds"] == before["rounds"]
    assert after["slots"] == before["slots"]
    assert after["summary"]["current_revision"] == 2
    assert after["summary"]["entry_fee_alpha_rao"] == 3_000_000_000
    assert after["summary"]["published_at"] == before["summary"]["published_at"]
    assert after["summary"]["updated_at"] == after["revisions"][1]["published_at"]
    assert [e["kind"] for e in after["events"]] == [
        "revision_published",
        "round_opened",
        "revision_published",
    ]


@pytest.mark.parametrize(
    "terms,refusal",
    [
        ({"entry_fee_alpha_rao": 0}, "entry fee must be a positive alpha amount"),
        ({"reward_pool_alpha_rao": 0}, "reward pool must be a positive alpha amount"),
        ({"attempts_per_round": 2}, "exactly 3 paid slots per round"),
        ({"funding_mode": "live"}, "live funding needs separate operator approval"),
        (
            {"fee_escrow_hotkey": TERMS["fee_escrow_coldkey"]},
            "coldkey and hotkey must be different keys",
        ),
        ({"title": "   "}, "title must not be blank"),
        ({"acceptance_evidence": "\n"}, "acceptance_evidence must not be blank"),
    ],
)
async def test_incomplete_terms_are_refused_by_preview_and_publish(
    app, client, session_maker, terms, refusal
):
    _install(app, session_maker)
    shown = await preview(client, terms=terms)
    assert any(refusal in item for item in shown["refusals"]), shown
    refused = await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_HEADERS,
        json=_request(terms=terms, confirmation=shown["confirmation"]),
    )
    assert refused.status_code == 409, refused.text
    assert refusal in refused.json()["message"]
    assert set((await counts(session_maker)).values()) == {0}


@pytest.mark.parametrize(
    "phrase",
    [
        "PUBLISH BOUNTY",
        f"PUBLISH BOUNTY {_REPO}#2942 REVISION 1 " + "ab" * 32,
        f"PUBLISH BOUNTY {_REPO}#2942 REVISION 2 {{digest}}",
    ],
)
async def test_publication_needs_the_exact_confirmation(
    app, client, session_maker, phrase
):
    _install(app, session_maker)
    digest = (await preview(client))["spec_digest"]
    refused = await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_HEADERS,
        json=_request(confirmation=phrase.replace("{digest}", digest)),
    )
    assert refused.status_code == 409, refused.text
    assert "confirmation" in refused.json()["message"]
    assert set((await counts(session_maker)).values()) == {0}


async def test_stale_expected_revisions_are_refused(app, client, session_maker):
    _install(app, session_maker)
    assert (await publish(client)).status_code == 200
    # Revision 1 exists: a second "first" revision and a skipped one both fail.
    for expected in (0, 2):
        shown = await preview(client, expected=expected)
        assert any("current revision is 1" in item for item in shown["refusals"])
        refused = await client.post(
            "/api/v1/admin/bounties/revisions",
            headers=_HEADERS,
            json=_request(expected=expected, confirmation=shown["confirmation"]),
        )
        assert refused.status_code == 409, refused.text
        assert "revision changed" in refused.json()["message"]
    assert (await counts(session_maker))["bounty_revisions"] == 1


async def test_concurrent_publications_of_one_revision_have_one_winner(
    app, client, session_maker
):
    _install(app, session_maker)
    phrase = (await preview(client))["confirmation"]
    results = await asyncio.gather(
        *(
            client.post(
                "/api/v1/admin/bounties/revisions",
                headers=_HEADERS,
                json=_request(confirmation=phrase),
            )
            for _ in range(4)
        )
    )
    assert sorted(result.status_code for result in results) == [200, 409, 409, 409]
    assert await counts(session_maker) == {
        "bounties": 1,
        "bounty_revisions": 1,
        "bounty_rounds": 1,
        "bounty_slots": 3,
        "bounty_events": 2,
    }


async def test_public_reads_page_cache_and_hide_private_operator_fields(
    app, client, session_maker
):
    _install(app, session_maker)
    for issue in (101, 102, 103):
        assert (await publish(client, issue=issue)).status_code == 200
    first = await client.get("/api/v1/public/bounties", params={"limit": 2})
    rest = await client.get("/api/v1/public/bounties", params={"limit": 2, "offset": 2})
    assert first.headers["cache-control"].startswith("public, max-age=")
    assert first.json()["total"] == rest.json()["total"] == 3
    issues = [item["issue"] for item in first.json()["items"] + rest.json()["items"]]
    assert sorted(issues) == [101, 102, 103] and len(first.json()["items"]) == 2
    # One issue's bounty is found without paging.
    one = await client.get(
        "/api/v1/public/bounties", params={"repo": _REPO, "issue": 102}
    )
    assert [item["issue"] for item in one.json()["items"]] == [102]
    assert one.json()["total"] == 1
    other = await client.get(
        "/api/v1/public/bounties", params={"repo": "ditto-assistant/other"}
    )
    assert other.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    for params in ({"repo": "Ditto-Assistant/ditto-subnet"}, {"issue": 0}):
        bad = await client.get("/api/v1/public/bounties", params=params)
        assert bad.status_code == 422, bad.text
    detail = await client.get(
        f"/api/v1/public/bounties/{first.json()['items'][0]['bounty_id']}"
    )
    assert detail.headers["cache-control"].startswith("public, max-age=")
    for response in (first, rest, detail):
        # Operator identities and publication reasons stay private.
        assert _REVIEWER not in response.text
        assert _ACTOR not in response.text
        assert "publish a test-only fixture bounty" not in response.text


async def test_unknown_and_malformed_bounty_ids(app, client, session_maker):
    _install(app, session_maker)
    missing = await client.get(
        "/api/v1/public/bounties/00000000-0000-4000-8000-000000000000"
    )
    assert missing.status_code == 404, missing.text
    assert HttpErrorEnvelope.model_validate(missing.json()).message == (
        "bounty not found"
    )
    assert missing.headers["cache-control"] == "no-store"
    malformed = await client.get("/api/v1/public/bounties/not-a-uuid")
    assert malformed.status_code == 422, malformed.text


async def test_admin_routes_require_the_token_and_a_named_actor(
    app, client, session_maker
):
    _install(app, session_maker)
    phrase = (await preview(client))["confirmation"]
    anonymous = await client.post(
        "/api/v1/admin/bounties/revisions", json=_request(confirmation=phrase)
    )
    assert anonymous.status_code == 401, anonymous.text
    unnamed = await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_ADMIN_TOKEN,
        json=_request(confirmation=phrase),
    )
    assert unnamed.status_code == 422 and "X-Admin-Actor" in unnamed.text
    blank_reviewer = await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_HEADERS,
        json=_request(confirmation=phrase, reviewer_actor="   "),
    )
    assert blank_reviewer.status_code == 400, blank_reviewer.text
    coerced = await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_HEADERS,
        json=_request(confirmation=phrase, terms={"entry_fee_alpha_rao": "2000000000"}),
    )
    assert coerced.status_code == 422, coerced.text
    inexact = await client.post(
        "/api/v1/admin/bounties/revisions",
        headers=_HEADERS,
        json=_request(confirmation=phrase, terms={"reward_pool_alpha_rao": 2**53}),
    )
    assert inexact.status_code == 422, inexact.text
    assert set((await counts(session_maker)).values()) == {0}


async def test_one_issue_has_one_bounty_whatever_the_spelling(
    app, client, session_maker
):
    _install(app, session_maker)
    assert (await publish(client)).status_code == 200
    body = _request(confirmation="PUBLISH BOUNTY")
    body["repo"] = "Ditto-Assistant/Ditto-Subnet"
    for route in ("preview", "revisions"):
        refused = await client.post(
            f"/api/v1/admin/bounties/{route}", headers=_HEADERS, json=body
        )
        assert refused.status_code == 422, refused.text
    async with session_maker() as session:
        with pytest.raises(DBAPIError, match="ck_bounties_repo"):
            await session.execute(
                text(
                    "INSERT INTO bounties (bounty_id, repo, issue_number, created_at) "
                    "VALUES (gen_random_uuid(), 'Ditto-Assistant/ditto-subnet', "
                    "2942, now())"
                )
            )
    assert (await counts(session_maker))["bounties"] == 1


async def test_published_history_is_append_only(app, client, session_maker):
    _install(app, session_maker)
    assert (await publish(client)).status_code == 200
    history = "bounty history is append-only"
    blocked = {
        "UPDATE bounties SET issue_number = 1": history,
        "DELETE FROM bounties": history,
        "UPDATE bounty_revisions SET entry_fee_alpha_rao = 1": history,
        "DELETE FROM bounty_revisions": history,
        "UPDATE bounty_rounds SET reason = 'rewritten history'": history,
        "DELETE FROM bounty_rounds": history,
        "UPDATE bounty_events SET kind = 'round_opened'": history,
        "DELETE FROM bounty_events": history,
        "UPDATE bounty_slots SET position = 9": "only a bounty slot status can change",
        "DELETE FROM bounty_slots": "bounty slots cannot be deleted",
        "TRUNCATE bounties CASCADE": history,
        "TRUNCATE bounty_slots CASCADE": history,
        "TRUNCATE bounty_events CASCADE": history,
    }
    for statement, refusal in blocked.items():
        async with session_maker() as session:
            # The trigger refuses it, not a schema error.
            with pytest.raises(DBAPIError, match=refusal):
                await session.execute(text(statement))
            await session.rollback()
    assert await counts(session_maker) == {
        "bounties": 1,
        "bounty_revisions": 1,
        "bounty_rounds": 1,
        "bounty_slots": 3,
        "bounty_events": 2,
    }
    async with session_maker() as session, session.begin():
        # A slot's status may move (P2 adds its claim and deadline).
        await session.execute(
            text(
                "UPDATE bounty_slots SET status = 'cancelled', "
                "status_changed_at = now() WHERE position = 1"
            )
        )
    async with session_maker() as session:
        statuses = sorted(await session.scalars(select(BountySlot.status)))
    assert statuses == ["cancelled", "open", "open"]


async def test_publication_routes_declare_their_errors(app):
    paths = app.openapi()["paths"]
    assert {"400", "401", "409", "503"} <= set(
        paths["/api/v1/admin/bounties/revisions"]["post"]["responses"]
    )
    assert {"401", "503"} <= set(
        paths["/api/v1/admin/bounties/preview"]["post"]["responses"]
    )
    assert "404" in paths["/api/v1/public/bounties/{bounty_id}"]["get"]["responses"]
