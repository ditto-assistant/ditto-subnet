"""Malformed transport bytes must not starve valid durable requests."""

import base64
import json
from types import SimpleNamespace

import pytest

from ditto_screening_protocol.treasury_pubsub import TreasuryMailbox


@pytest.mark.parametrize(
    "data",
    [
        "!not-base64!",
        base64.b64encode(b"not-json").decode(),
        base64.b64encode(b"[]").decode(),
        base64.b64encode(b"x" * 65537).decode(),
    ],
)
def test_undecodable_message_is_acknowledged_without_dispatch(data, capsys):
    acknowledged = []
    mailbox = SimpleNamespace(
        subscription="requests",
        ack=acknowledged.append,
        _call=lambda *_: {
            "receivedMessages": [{"ackId": "bad-message", "message": {"data": data}}]
        },
    )
    assert TreasuryMailbox.pull(mailbox) is None
    assert acknowledged == ["bad-message"]
    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "mailbox_message_dropped"
    assert set(record) == {"status", "error_type"}
    assert record["error_type"] in {"Error", "JSONDecodeError", "ValueError"}


def test_valid_object_keeps_ack_with_durable_consumer():
    acknowledged = []
    mailbox = SimpleNamespace(
        subscription="requests",
        ack=acknowledged.append,
        _call=lambda *_: {
            "receivedMessages": [
                {
                    "ackId": "valid",
                    "message": {
                        "data": base64.b64encode(b'{"request_id":"bound"}').decode()
                    },
                }
            ]
        },
    )
    assert TreasuryMailbox.pull(mailbox) == ("valid", {"request_id": "bound"})
    assert acknowledged == []


def test_malformed_message_ack_failure_is_not_hidden():
    def failed_ack(_):
        raise TimeoutError("delivery unknown")

    mailbox = SimpleNamespace(
        subscription="requests",
        ack=failed_ack,
        _call=lambda *_: {
            "receivedMessages": [{"ackId": "bad", "message": {"data": "!"}}]
        },
    )
    with pytest.raises(TimeoutError, match="delivery unknown"):
        TreasuryMailbox.pull(mailbox)
