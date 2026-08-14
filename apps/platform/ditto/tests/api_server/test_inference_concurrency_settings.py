"""The resolver that carries a backroom write to the admission path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ditto.api_models.inference_concurrency_settings import (
    InferenceConcurrencySettings,
)
from ditto.api_server.inference_concurrency_settings import (
    DEFAULT_SETTINGS,
    InferenceConcurrencySettingsResolver,
    apply_settings,
    settings_from_row,
)
from ditto.db.queries.inference_concurrency_settings import (
    insert_inference_concurrency_settings_revision,
)
from ditto.tests.db.queries.test_inference import _config

pytestmark = pytest.mark.asyncio


# The local `engine` / `session_maker` fixtures are gone: the root
# conftest provides both names against a real Postgres, so this file
# migrates by deletion and every test signature stays as it was.


def _row(settings: object, revision: int = 1) -> Any:
    """A stand-in revision row.

    Typed ``Any`` deliberately: the point of these cases is to feed the decoder
    payloads a real row could hold but the ORM type does not describe -- a
    negative int, a bare string -- so annotating it as the ORM class would make
    the type checker reject the very inputs under test.
    """
    return SimpleNamespace(settings=settings, revision=revision)


class TestDecoding:
    async def test_missing_row_is_the_shipped_default(self) -> None:
        assert settings_from_row(None) == DEFAULT_SETTINGS

    async def test_corrupt_row_fails_open_onto_the_default(self) -> None:
        """A bad revision must not be able to stall or unbound the lane.

        The defaults are a working configuration, so failing open costs an
        operator their revision, never a run.
        """
        assert settings_from_row(_row({"embedding_per_ticket_concurrency": -5})) == (
            DEFAULT_SETTINGS
        )
        assert settings_from_row(_row("not-an-object")) == DEFAULT_SETTINGS

    async def test_valid_row_is_decoded(self) -> None:
        decoded = settings_from_row(
            _row(
                {
                    "embedding_per_ticket_concurrency": 4,
                    "embedding_per_validator_concurrency": 16,
                    "embedding_global_concurrency": 32,
                }
            )
        )
        assert decoded.embedding_per_ticket_concurrency == 4


class TestApplySettings:
    async def test_only_the_board_owned_fields_move(self) -> None:
        """The overlay must not disturb anything this board does not own.

        Written as a whole-object diff rather than a handful of equality
        assertions so that a future field added to the settings model, and wired
        into ``apply_settings`` by mistake, fails here instead of silently
        retuning something unrelated.

        ``request_budget`` and ``token_budget`` are both owned deliberately.
        Note what is *not*: the chat rate limits and the embedding token
        budget. The chat token budget moved onto the board
        because leaving it boot-time is what made #473 inert -- the operator
        raised the request budget from backroom and the number that was actually
        binding could only be changed by a redeploy.
        """
        config = _config()
        overlaid = apply_settings(
            config,
            InferenceConcurrencySettings(
                chat_request_budget=4096,
                chat_token_budget=12_000_000,
                chat_per_ticket_concurrency=2,
                chat_per_validator_concurrency=3,
                chat_global_concurrency=4,
                embedding_per_ticket_concurrency=12,
                embedding_per_validator_concurrency=48,
                embedding_global_concurrency=96,
            ),
        )
        changed = {
            field
            for field in vars(config)
            if getattr(config, field) != getattr(overlaid, field)
        }
        assert changed == {
            "request_budget",
            "token_budget",
            "per_ticket_concurrency",
            "per_validator_concurrency",
            "global_concurrency",
            "embedding_per_ticket_concurrency",
            "embedding_per_validator_concurrency",
            "embedding_global_concurrency",
        }
        assert overlaid.request_budget == 4096
        assert overlaid.token_budget == 12_000_000
        assert overlaid.per_ticket_concurrency == 2
        assert overlaid.per_validator_concurrency == 3
        assert overlaid.global_concurrency == 4
        assert overlaid.embedding_token_budget == config.embedding_token_budget


class TestResolver:
    async def test_no_session_maker_serves_defaults(self) -> None:
        resolver = InferenceConcurrencySettingsResolver()
        assert await resolver.resolve(None) == DEFAULT_SETTINGS

    async def test_a_written_revision_governs(self, session_maker) -> None:
        resolver = InferenceConcurrencySettingsResolver(ttl_seconds=0)
        assert (await resolver.resolve(session_maker)).embedding_per_ticket_concurrency
        async with session_maker() as session, session.begin():
            await insert_inference_concurrency_settings_revision(
                session,
                parent_revision=0,
                scope="*",
                settings={
                    "embedding_per_ticket_concurrency": 3,
                    "embedding_per_validator_concurrency": 6,
                    "embedding_global_concurrency": 9,
                },
                checksum="b" * 64,
                reason="throttle the hosted embedding lane while observing",
                actor="tester",
            )
        resolved = await resolver.resolve(session_maker)
        assert resolved.embedding_per_ticket_concurrency == 3

    async def test_cache_holds_then_invalidate_releases_it(self, session_maker) -> None:
        """The TTL is what keeps a per-request read off the hot path.

        671 embeddings per v7 run means an uncached resolve would add a SELECT
        to the most frequent operation in the service. The invalidate hook is
        what stops an operator reading their own write as stale.
        """
        resolver = InferenceConcurrencySettingsResolver(ttl_seconds=3600)
        first = await resolver.resolve(session_maker)
        assert first == DEFAULT_SETTINGS
        async with session_maker() as session, session.begin():
            await insert_inference_concurrency_settings_revision(
                session,
                parent_revision=0,
                scope="*",
                settings={
                    "embedding_per_ticket_concurrency": 2,
                    "embedding_per_validator_concurrency": 2,
                    "embedding_global_concurrency": 2,
                },
                checksum="c" * 64,
                reason="pull the emergency brake on embedding concurrency",
                actor="tester",
            )
        assert await resolver.resolve(session_maker) == DEFAULT_SETTINGS
        resolver.invalidate()
        assert (
            await resolver.resolve(session_maker)
        ).embedding_per_ticket_concurrency == 2
