"""Guard: a fresh checkout with no ``.env`` must be able to build the config.

This is the test that keeps ``ditto/tests/env_defaults.py`` honest. It does
not check that the suite happens to be green on *this* machine -- a machine
with ``.env`` exported would pass that trivially, which is exactly how the
gap went unnoticed. Instead it rebuilds the environment from scratch,
containing only what a pristine clone would have (nothing) plus what the
test layer promises to provide, and then runs every ``parse_*_from_env``
parser against it.

So if someone adds a newly-required environment variable to any config
parser and does not add a test default for it, this test fails and names
the variable, instead of eleven unrelated tests failing somewhere else with
a stack trace the next person has to triage from first principles.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

import pytest

from ditto.api_server.config import check_config, parse_api_server_config_from_env
from ditto.api_server.datapipeline.config import parse_data_pipeline_config_from_env
from ditto.api_server.embedding.config import parse_embedding_config_from_env
from ditto.api_server.pricing.config import parse_pricing_config_from_env
from ditto.api_server.storage.models import parse_storage_config_from_env
from ditto.api_server.validator_names import parse_validator_names_config_from_env
from ditto.chain.models import parse_chain_config_from_env
from ditto.db.config import parse_postgres_config_from_env
from ditto.tests.env_defaults import (
    RUNTIME_PROVIDED_ENV,
    TEST_ENV_DEFAULTS,
    apply_test_env_defaults,
)

_WHERE_TO_FIX = (
    "Add a default for it to TEST_ENV_DEFAULTS in ditto/tests/env_defaults.py "
    "(test layer only -- do NOT give the production parser a fallback; a "
    "platform that boots on a placeholder is worse than a red test)."
)

# Each parser the app's boot path depends on. `parse_api_server_config_from_env`
# already calls most of these transitively, but naming them individually means
# a failure points at the parser that is missing a value rather than at the
# aggregate.
PARSERS: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("postgres", parse_postgres_config_from_env),
    ("chain", parse_chain_config_from_env),
    ("storage", parse_storage_config_from_env),
    ("pricing", parse_pricing_config_from_env),
    ("embedding", parse_embedding_config_from_env),
    ("data pipeline", parse_data_pipeline_config_from_env),
    ("validator names", parse_validator_names_config_from_env),
)


@pytest.fixture
def pristine_env(worker_database: object) -> Iterator[None]:
    """``os.environ`` as a fresh clone would have it, plus the test defaults.

    Cleared to empty first, so a developer's ``.env`` or an exported shell
    variable cannot mask a missing default. ``POSTGRES_*`` is carried over
    from the live environment because the ``worker_database`` fixture -- not
    the static defaults -- is what supplies this xdist worker's own database
    address, and requesting that fixture is what guarantees it has run.
    """
    del worker_database
    carried = {
        name: os.environ[name] for name in RUNTIME_PROVIDED_ENV if name in os.environ
    }
    missing_runtime = [name for name in RUNTIME_PROVIDED_ENV if name not in carried]
    assert not missing_runtime, (
        f"the worker_database fixture did not export {missing_runtime} into "
        "os.environ; the per-worker database DSN is set in the "
        "`worker_database` fixture in ditto/tests/conftest.py"
    )
    with mock.patch.dict(os.environ, carried, clear=True):
        apply_test_env_defaults()
        yield


class TestEveryRequiredEnvVarHasATestDefault:
    """The regression guard proper."""

    @pytest.mark.parametrize("name,parser", PARSERS, ids=[n for n, _ in PARSERS])
    def test_parser_succeeds_on_a_pristine_environment(
        self,
        name: str,
        parser: Callable[[], Any],
        pristine_env: None,
    ) -> None:
        del pristine_env
        try:
            parser()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.fail(
                f"the {name} config parser requires an environment variable "
                f"the test suite does not provide.\n"
                f"  {type(exc).__name__}: {exc}\n"
                f"{_WHERE_TO_FIX}"
            )

    def test_the_whole_api_server_config_builds_and_validates(
        self, pristine_env: None
    ) -> None:
        """The aggregate parser plus ``check_config``.

        ``check_config`` is included because a value can be present and
        still be rejected -- a default that parses but fails validation
        would fail the suite just as thoroughly as a missing one.
        """
        del pristine_env
        try:
            config = parse_api_server_config_from_env(commit_hash="env-default-guard")
            check_config(config)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            pytest.fail(
                "the api_server config cannot be built from a pristine "
                "environment, so `uv run pytest` on a fresh clone with no "
                f".env is red.\n  {type(exc).__name__}: {exc}\n{_WHERE_TO_FIX}"
            )


class TestDefaultsAreTestFixturesNotRealValues:
    """The defaults must stay obviously fake, and must never win over real ones."""

    def test_the_payment_address_is_an_unmistakable_sentinel(self) -> None:
        address = TEST_ENV_DEFAULTS["DITTO_UPLOAD_PAYMENT_ADDRESS"]
        # Reads as "not a real SS58 address, test fixture, do not send TAO
        # here". If this ever becomes a plausible-looking key, the sentinel
        # has stopped doing its job.
        assert "NotARea1" in address
        assert "TestFixture" in address
        assert "DoNotSendTao" in address

    def test_an_explicit_value_is_never_overwritten(self) -> None:
        """A developer's ``.env`` and CI's explicit block must still win."""
        real = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        with mock.patch.dict(
            os.environ, {"DITTO_UPLOAD_PAYMENT_ADDRESS": real}, clear=False
        ):
            apply_test_env_defaults()
            assert os.environ["DITTO_UPLOAD_PAYMENT_ADDRESS"] == real

    def test_production_still_fails_loudly_on_a_missing_required_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defaults are seeded into ``os.environ``, not into the parser.

        That distinction is the entire safety argument for this change, so
        it gets a test: delete the variable and production must still
        refuse to build a config rather than substitute a placeholder.
        """
        from ditto.api_server.errors import ApiServerConfigError

        monkeypatch.delenv("DITTO_UPLOAD_PAYMENT_ADDRESS", raising=False)
        with pytest.raises(ApiServerConfigError, match="DITTO_UPLOAD_PAYMENT_ADDRESS"):
            parse_api_server_config_from_env(commit_hash="abc")
