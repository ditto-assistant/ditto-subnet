"""Malformed transport bytes must not starve valid durable requests."""

import base64
import json
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

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


@pytest.mark.parametrize("response", [b"", b"{}"])
def test_successful_empty_ack_response_is_accepted(response):
    replies = iter([b'{"access_token":"fixture-token"}', response])
    calls = []

    def open_request(request, *, timeout):
        assert timeout == 30
        calls.append(request.full_url)
        assert timeout == 30
        return BytesIO(next(replies))

    mailbox = TreasuryMailbox(
        project="fixture-project", topic="manual-topic", subscription="manual-results"
    )
    mailbox.opener = SimpleNamespace(open=open_request)
    mailbox.ack("fixture-ack")
    assert calls[-1].endswith("subscriptions/manual-results:acknowledge")


@pytest.mark.parametrize("response", [b"[]", b"null", b'"not-object"'])
def test_non_object_ack_response_remains_rejected(response):
    replies = iter([b'{"access_token":"fixture-token"}', response])
    mailbox = TreasuryMailbox(
        project="fixture-project", topic="manual-topic", subscription="manual-results"
    )
    mailbox.opener = SimpleNamespace(open=lambda *_a, **_k: BytesIO(next(replies)))
    with pytest.raises(ValueError, match="object required"):
        mailbox.ack("fixture-ack")


@pytest.mark.parametrize("method", ["publish", "pull"])
def test_empty_non_ack_response_is_not_silently_accepted(method):
    replies = iter([b'{"access_token":"fixture-token"}', b""])
    mailbox = TreasuryMailbox(
        project="fixture-project", topic="manual-topic", subscription="manual-results"
    )
    mailbox.opener = SimpleNamespace(open=lambda *_a, **_k: BytesIO(next(replies)))
    with pytest.raises(json.JSONDecodeError):
        mailbox._call(mailbox.subscription, method, {})


def test_metadata_token_reused_then_refreshed_before_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "ditto_screening_protocol.treasury_pubsub.monotonic", lambda: now[0]
    )
    requests = []
    metadata_reads = []

    def open_request(request, *, timeout):
        assert timeout == 30
        requests.append(request)
        if request.full_url.startswith("http://metadata."):
            metadata_reads.append(request)
            return BytesIO(
                json.dumps(
                    {
                        "access_token": f"fixture-{len(metadata_reads)}",
                        "expires_in": 120,
                    }
                ).encode()
            )
        return BytesIO(b"{}")

    mailbox = TreasuryMailbox(
        project="fixture-project", topic="manual-topic", subscription="manual-results"
    )
    mailbox.opener = SimpleNamespace(open=open_request)
    mailbox.ack("first")
    now[0] = 159.0
    mailbox.ack("second")
    assert len(metadata_reads) == 1
    now[0] = 160.0
    mailbox.ack("third")
    assert len(metadata_reads) == 2
    assert (
        requests[1].get_header("Authorization")
        == requests[2].get_header("Authorization")
        == "Bearer fixture-1"
    )
    assert requests[-1].get_header("Authorization") == "Bearer fixture-2"


@pytest.mark.parametrize("lifetime", [None, 0, 60, "3600", True])
def test_unknown_or_short_token_lifetime_is_not_cached(lifetime):
    metadata_reads = []

    def open_request(request, *, timeout):
        assert timeout == 30
        if request.full_url.startswith("http://metadata."):
            metadata_reads.append(request)
            return BytesIO(
                json.dumps({"access_token": "fixture", "expires_in": lifetime}).encode()
            )
        return BytesIO(b"{}")

    mailbox = TreasuryMailbox(
        project="fixture-project", topic="manual-topic", subscription="manual-results"
    )
    mailbox.opener = SimpleNamespace(open=open_request)
    mailbox.ack("first")
    mailbox.ack("second")
    assert len(metadata_reads) == 2


def test_auth_failure_invalidates_token_without_in_call_retry():
    calls = []
    fail = [True]

    def open_request(request, *, timeout):
        assert timeout == 30
        calls.append(request.full_url)
        if request.full_url.startswith("http://metadata."):
            return BytesIO(b'{"access_token":"fixture", "expires_in":3600}')
        if fail[0]:
            raise HTTPError(request.full_url, 401, "unauthorized", {}, None)
        return BytesIO(b"{}")

    mailbox = TreasuryMailbox(
        project="fixture-project", topic="manual-topic", subscription="manual-results"
    )
    mailbox.opener = SimpleNamespace(open=open_request)
    with pytest.raises(HTTPError):
        mailbox.ack("first")
    assert len(calls) == 2 and mailbox._cached_token is None
    fail[0] = False
    mailbox.ack("second")
    assert len(calls) == 4
