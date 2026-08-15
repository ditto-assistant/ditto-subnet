"""The hosted-embedding concurrency board's contract.

Two things are pinned here that a future edit could quietly undo:

* the shipped defaults are a **raise**, not the old serialised values, so the
  improvement cannot be reduced to an opt-in knob by editing one number; and
* the board's ceilings are exactly the ceiling ``check_config`` enforces at
  boot, so the two validators can never disagree about what is acceptable.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ditto.api_models.inference_concurrency_settings import (
    DEFAULT_CHAT_GLOBAL_CONCURRENCY,
    DEFAULT_CHAT_PER_TICKET_CONCURRENCY,
    DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY,
    DEFAULT_CHAT_REQUEST_BUDGET,
    DEFAULT_CHAT_TOKEN_BUDGET,
    DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY,
    DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY,
    DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY,
    MAX_CHAT_CONCURRENCY,
    MAX_CHAT_TOKEN_BUDGET,
    MAX_EMBEDDING_GLOBAL_CONCURRENCY,
    MAX_EMBEDDING_PER_TICKET_CONCURRENCY,
    MAX_EMBEDDING_PER_VALIDATOR_CONCURRENCY,
    AdminInferenceConcurrencySettingsRequest,
    BenchmarkRuntimeSettings,
    InferenceConcurrencySettings,
)

# The values the hosted v7 lane ran at while it was still sized for a local
# Ollama container. Named here so the assertion below reads as the claim it is.
VESTIGIAL_OLLAMA_ERA_LIMITS = (1, 8, 32)


class TestDefaults:
    def test_defaults_are_a_raise_not_the_old_serialised_values(self) -> None:
        settings = InferenceConcurrencySettings()
        assert (
            settings.embedding_per_ticket_concurrency,
            settings.embedding_per_validator_concurrency,
            settings.embedding_global_concurrency,
        ) != VESTIGIAL_OLLAMA_ERA_LIMITS
        # Each one strictly above what it replaced: shipping a board whose
        # default reproduces the old behaviour would make the improvement
        # opt-in, which is the failure mode this change exists to avoid.
        assert settings.embedding_per_ticket_concurrency > 1
        assert settings.embedding_per_validator_concurrency > 8
        assert settings.embedding_global_concurrency > 32

    def test_defaults_match_the_documented_constants(self) -> None:
        settings = InferenceConcurrencySettings()
        assert (
            settings.chat_per_ticket_concurrency == DEFAULT_CHAT_PER_TICKET_CONCURRENCY
        )
        assert (
            settings.chat_per_validator_concurrency
            == DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY
        )
        assert settings.chat_global_concurrency == DEFAULT_CHAT_GLOBAL_CONCURRENCY
        assert (
            settings.embedding_per_ticket_concurrency
            == DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY
        )
        assert (
            settings.embedding_per_validator_concurrency
            == DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY
        )
        assert (
            settings.embedding_global_concurrency
            == DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY
        )

    def test_board_ceiling_matches_the_relay_hard_ceiling(self) -> None:
        """The API must reject values the Go admission process cannot enforce."""
        assert MAX_EMBEDDING_GLOBAL_CONCURRENCY == 512
        assert MAX_CHAT_CONCURRENCY == 512

    def test_chat_token_ceiling_has_room_for_the_measured_tail(self) -> None:
        assert DEFAULT_CHAT_TOKEN_BUDGET == 25_000_000
        assert MAX_CHAT_TOKEN_BUDGET == 100_000_000
        assert (
            InferenceConcurrencySettings(
                chat_token_budget=MAX_CHAT_TOKEN_BUDGET
            ).chat_token_budget
            == MAX_CHAT_TOKEN_BUDGET
        )
        with pytest.raises(ValidationError, match="less than or equal"):
            InferenceConcurrencySettings(chat_token_budget=MAX_CHAT_TOKEN_BUDGET + 1)

    def test_v10_runtime_defaults_preserve_deployed_behavior(self) -> None:
        runtime = InferenceConcurrencySettings().benchmark_runtime
        assert runtime.case_concurrency == 1
        assert runtime.relay_delay_fingerprint_mode == "off"

    def test_v10_runtime_bounds_are_fail_closed(self) -> None:
        with pytest.raises(ValidationError):
            BenchmarkRuntimeSettings(case_concurrency=17)
        with pytest.raises(ValidationError, match="may not exceed"):
            BenchmarkRuntimeSettings(
                relay_delay_fingerprint_min_ms=300,
                relay_delay_fingerprint_max_ms=100,
            )


class TestHierarchy:
    def test_chat_ticket_may_not_exceed_validator(self) -> None:
        with pytest.raises(ValidationError, match="chat_per_ticket_concurrency"):
            InferenceConcurrencySettings(
                chat_per_ticket_concurrency=64,
                chat_per_validator_concurrency=32,
                chat_global_concurrency=96,
            )

    def test_chat_validator_may_not_exceed_global(self) -> None:
        with pytest.raises(ValidationError, match="chat_per_validator_concurrency"):
            InferenceConcurrencySettings(
                chat_per_ticket_concurrency=16,
                chat_per_validator_concurrency=96,
                chat_global_concurrency=64,
            )

    def test_ticket_may_not_exceed_validator(self) -> None:
        with pytest.raises(ValidationError, match="may not exceed"):
            InferenceConcurrencySettings(
                embedding_per_ticket_concurrency=16,
                embedding_per_validator_concurrency=8,
                embedding_global_concurrency=64,
            )

    def test_validator_may_not_exceed_global(self) -> None:
        with pytest.raises(ValidationError, match="may not exceed"):
            InferenceConcurrencySettings(
                embedding_per_ticket_concurrency=4,
                embedding_per_validator_concurrency=64,
                embedding_global_concurrency=16,
            )

    def test_equal_limits_are_allowed(self) -> None:
        settings = InferenceConcurrencySettings(
            embedding_per_ticket_concurrency=8,
            embedding_per_validator_concurrency=8,
            embedding_global_concurrency=8,
        )
        assert settings.embedding_global_concurrency == 8

    def test_zero_is_refused(self) -> None:
        """A lane of zero would stall every v7 run rather than slow it."""
        with pytest.raises(ValidationError):
            InferenceConcurrencySettings(embedding_per_ticket_concurrency=0)

    def test_above_ceiling_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            InferenceConcurrencySettings(
                embedding_per_ticket_concurrency=513,
                embedding_per_validator_concurrency=513,
                embedding_global_concurrency=513,
            )


class TestWriteContract:
    def _request(self, **settings: Any) -> AdminInferenceConcurrencySettingsRequest:
        return AdminInferenceConcurrencySettingsRequest(
            expected_revision=0,
            settings=InferenceConcurrencySettings(**settings),
            reason="widen the hosted embedding lane",
            confirmation="APPLY INFERENCE CONCURRENCY SETTINGS",
        )

    def test_partial_policy_is_refused_with_the_missing_fields_named(self) -> None:
        with pytest.raises(ValidationError, match="embedding_per_ticket_concurrency"):
            self._request(
                embedding_per_validator_concurrency=48,
                embedding_global_concurrency=96,
            )

    def test_complete_policy_is_accepted(self) -> None:
        request = self._request(
            chat_request_budget=8192,
            chat_token_budget=25_000_000,
            chat_per_ticket_concurrency=16,
            chat_per_validator_concurrency=48,
            chat_global_concurrency=96,
            embedding_per_ticket_concurrency=16,
            embedding_per_validator_concurrency=64,
            embedding_global_concurrency=128,
        )
        assert request.settings.embedding_per_ticket_concurrency == 16
        assert request.settings.chat_request_budget == 8192

    def test_a_write_omitting_only_the_chat_budget_is_refused(self) -> None:
        """The whole-object guard has to cover the newest field too.

        This is the concrete footgun: an operator adjusting the embedding lane
        from a remembered payload would otherwise silently reset the chat
        request budget to its default, and `expected_revision` cannot catch it
        because they do hold the current revision.
        """
        with pytest.raises(ValidationError, match="chat_request_budget"):
            self._request(
                chat_token_budget=25_000_000,
                chat_per_ticket_concurrency=16,
                chat_per_validator_concurrency=48,
                chat_global_concurrency=96,
                embedding_per_ticket_concurrency=16,
                embedding_per_validator_concurrency=64,
                embedding_global_concurrency=128,
            )

    def test_a_write_omitting_only_the_token_budget_is_refused(self) -> None:
        """Same guard, same reason, for the field that actually bound v7.

        Worth its own case rather than folding into the one above: the token
        budget is the newest field, so it is the one a remembered payload is
        most likely to be missing, and silently resetting it to the default is
        precisely how a deliberate operator raise would evaporate.
        """
        with pytest.raises(ValidationError, match="chat_token_budget"):
            self._request(
                chat_request_budget=8192,
                chat_per_ticket_concurrency=16,
                chat_per_validator_concurrency=48,
                chat_global_concurrency=96,
                embedding_per_ticket_concurrency=16,
                embedding_per_validator_concurrency=64,
                embedding_global_concurrency=128,
            )


class TestCeilingMatchesMultiSlotFleet:
    """The hard ceiling leaves room for the full multi-validator slot fleet."""

    VALIDATOR_SLOTS = 8
    OPERATING_PER_TICKET = 32
    OPERATING_PER_VALIDATOR = 256
    OPERATING_GLOBAL = 512

    def test_chat_and_embedding_accept_the_same_operating_distribution(self) -> None:
        settings = InferenceConcurrencySettings(
            chat_per_ticket_concurrency=self.OPERATING_PER_TICKET,
            chat_per_validator_concurrency=self.OPERATING_PER_VALIDATOR,
            chat_global_concurrency=self.OPERATING_GLOBAL,
            embedding_per_ticket_concurrency=self.OPERATING_PER_TICKET,
            embedding_per_validator_concurrency=self.OPERATING_PER_VALIDATOR,
            embedding_global_concurrency=self.OPERATING_GLOBAL,
        )
        assert (
            (
                settings.chat_per_ticket_concurrency,
                settings.chat_per_validator_concurrency,
                settings.chat_global_concurrency,
            )
            == (
                settings.embedding_per_ticket_concurrency,
                settings.embedding_per_validator_concurrency,
                settings.embedding_global_concurrency,
            )
            == (32, 256, 512)
        )

    def test_one_validator_can_run_every_slot_at_operating_concurrency(self) -> None:
        assert MAX_EMBEDDING_PER_VALIDATOR_CONCURRENCY >= (
            self.VALIDATOR_SLOTS * self.OPERATING_PER_TICKET
        )

    def test_two_full_validators_fit_below_the_global_ceiling(self) -> None:
        assert MAX_EMBEDDING_GLOBAL_CONCURRENCY >= (
            2 * self.VALIDATOR_SLOTS * self.OPERATING_PER_TICKET
        )

    def test_the_three_ceilings_stay_equal(self) -> None:
        """One number, stated three times, so the hierarchy can always be flat.

        The invariant is ``per_ticket <= per_validator <= global``. Equal
        ceilings are what let an operator set all three to the same value --
        which is what the current production revision does -- without the board
        rejecting its own maximum.
        """
        assert (
            MAX_EMBEDDING_PER_TICKET_CONCURRENCY
            == MAX_EMBEDDING_PER_VALIDATOR_CONCURRENCY
            == MAX_EMBEDDING_GLOBAL_CONCURRENCY
            == MAX_CHAT_CONCURRENCY
            == 512
        )

    def test_the_flat_maximum_revision_is_accepted(self) -> None:
        """The shared 512/512/512 hard maximum is a legal policy."""
        settings = InferenceConcurrencySettings(
            chat_request_budget=DEFAULT_CHAT_REQUEST_BUDGET,
            chat_token_budget=DEFAULT_CHAT_TOKEN_BUDGET,
            chat_per_ticket_concurrency=DEFAULT_CHAT_PER_TICKET_CONCURRENCY,
            chat_per_validator_concurrency=DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY,
            chat_global_concurrency=DEFAULT_CHAT_GLOBAL_CONCURRENCY,
            embedding_per_ticket_concurrency=MAX_EMBEDDING_PER_TICKET_CONCURRENCY,
            embedding_per_validator_concurrency=MAX_EMBEDDING_PER_VALIDATOR_CONCURRENCY,
            embedding_global_concurrency=MAX_EMBEDDING_GLOBAL_CONCURRENCY,
        )
        assert settings.embedding_global_concurrency == 512


class TestChatBudgetsSurviveAnEmbeddingOnlyEdit:
    """The whole-policy store is a footgun with one specific victim.

    A revision replaces the entire object, so an operator who reads the board,
    changes only an embedding limit, and writes it back must carry both chat
    budgets through untouched. They are currently 8192 / 25,000,000 and are
    explicitly off-limits: legitimate agents run near them and the token
    accounting has known gaps, so a silent reset to a default would be
    indistinguishable from a deliberate cut.
    """

    def test_editing_an_embedding_limit_leaves_both_chat_budgets_alone(self) -> None:
        current = InferenceConcurrencySettings(
            chat_request_budget=8192,
            chat_token_budget=25_000_000,
            embedding_per_ticket_concurrency=48,
            embedding_per_validator_concurrency=96,
            embedding_global_concurrency=128,
        )
        # The read-modify-write an operator actually performs.
        updated = current.model_copy(update={"embedding_per_ticket_concurrency": 128})
        assert updated.embedding_per_ticket_concurrency == 128
        assert updated.chat_request_budget == 8192
        assert updated.chat_token_budget == 25_000_000

    def test_a_write_that_omits_the_chat_budgets_is_refused(self) -> None:
        """The guard that makes the above a habit rather than a hope.

        Omitting a field does not inherit it -- it defaults it. The request
        model refuses a partial policy for exactly this reason, and the error
        has to name what is missing or the operator cannot fix it.
        """
        with pytest.raises(ValidationError) as caught:
            AdminInferenceConcurrencySettingsRequest(
                expected_revision=3,
                settings=InferenceConcurrencySettings(
                    embedding_per_ticket_concurrency=128,
                    embedding_per_validator_concurrency=128,
                    embedding_global_concurrency=128,
                ),
                reason="raise the embedding lane",
                confirmation="APPLY",
            )
        message = str(caught.value)
        assert "chat_request_budget" in message
        assert "chat_token_budget" in message
