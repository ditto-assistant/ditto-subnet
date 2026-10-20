"""One Ditto OAuth connection, existing miner proof semantics."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from ditto.api_models.miner_ditto_link import MinerDittoLinkAttemptResponse
from ditto.miner_cli.commands.link_ditto import run
from ditto.tests.miner_cli.commands.test_link_ditto import (
    HOTKEY,
    SESSION,
    _args,
    _authenticated,
    _client,
    _view,
)


@pytest.mark.parametrize("existing", ["matching", "other-hotkey", "none"])
def test_connect_ditto_retains_proofs_and_only_signs_when_needed(existing):
    attempt_id = uuid4()
    client = _client(attempt_id, statuses=[_authenticated(attempt_id)])
    client.confirm_ditto_link.return_value = MinerDittoLinkAttemptResponse(
        attempt_id=attempt_id,
        status="linked",
        link=_view(),
        github_proof_attempt_id=attempt_id,
    )
    identity = SimpleNamespace(github_user_id=123, github_login="miner", hotkey=HOTKEY)
    client.get_github_link_attempt.return_value = identity
    client.get_github_link.return_value.link = (
        identity if existing == "matching" else None
    )
    client.get_github_account_link.return_value.link = (
        SimpleNamespace(hotkey="other") if existing == "other-hotkey" else None
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("ditto.miner_cli.commands.link_github.run", return_value=0) as sign,
    ):
        assert run(_args(yes=True, expect_github_id=123)) == 0
    if existing == "none":
        sign.assert_called_once()
        assert sign.call_args.args[0].ditto_attempt == attempt_id
        assert sign.call_args.args[0].expect_github_id == 123
    else:
        sign.assert_not_called()
    client.start_github_link.assert_not_called()
