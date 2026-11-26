from __future__ import annotations

import io
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from screener_capacity.aws import AwsCredentials, AwsError, EC2Primary, sigv4_headers

_INSTANCE = "i-0123456789abcdef0"
_ROLE = "arn:aws:iam::339713011188:role/ditto-screener-capacity-wake"
_ID_TOKEN = "header.payload.signature"


def _sts_xml(expires: datetime) -> bytes:
    return f"""<AssumeRoleWithWebIdentityResponse
  xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleWithWebIdentityResult>
    <Credentials>
      <AccessKeyId>ASIAEXAMPLE</AccessKeyId>
      <SecretAccessKey>secret</SecretAccessKey>
      <SessionToken>session</SessionToken>
      <Expiration>{expires.strftime("%Y-%m-%dT%H:%M:%SZ")}</Expiration>
    </Credentials>
  </AssumeRoleWithWebIdentityResult>
</AssumeRoleWithWebIdentityResponse>""".encode()


def _describe_xml(state: str, instance: str = _INSTANCE) -> bytes:
    return f"""<DescribeInstancesResponse
  xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <reservationSet><item><instancesSet><item>
    <instanceId>{instance}</instanceId>
    <instanceState><code>80</code><name>{state}</name></instanceState>
    <launchTime>2026-11-09T11:58:00.000Z</launchTime>
    <stateReason><code>Client.UserInitiatedShutdown</code></stateReason>
  </item></instancesSet></item></reservationSet>
</DescribeInstancesResponse>""".encode()


_START_XML = f"""<StartInstancesResponse
  xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <instancesSet><item><instanceId>{_INSTANCE}</instanceId>
    <currentState><name>pending</name></currentState>
  </item></instancesSet>
</StartInstancesResponse>""".encode()


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _Urlopen:
    """Route metadata, STS, and EC2 calls to canned responses."""

    def __init__(self, ec2: list[bytes]) -> None:
        self.ec2 = list(ec2)
        self.requests: list[Any] = []
        self.sts_calls = 0

    def __call__(self, request: Any, timeout: float) -> _Response:
        self.requests.append(request)
        url = request.full_url
        if url.startswith("http://metadata.google.internal/"):
            assert request.get_header("Metadata-flavor") == "Google"
            return _Response(_ID_TOKEN.encode())
        if url.startswith("https://sts."):
            self.sts_calls += 1
            return _Response(_sts_xml(datetime.now(UTC) + timedelta(minutes=15)))
        return _Response(self.ec2.pop(0))


def _primary() -> EC2Primary:
    return EC2Primary(
        region="us-east-1",
        instance_id=_INSTANCE,
        role_arn=_ROLE,
        audience="ditto-screener-capacity",
    )


class SigV4Tests(unittest.TestCase):
    def test_signature_matches_botocore_for_an_ec2_query(self) -> None:
        # Cross-checked against botocore.auth.SigV4Auth for the same request.
        headers = sigv4_headers(
            method="POST",
            host="ec2.us-east-1.amazonaws.com",
            path="/",
            query="",
            body=(
                b"Action=DescribeInstances&Version=2016-11-15"
                b"&InstanceId.1=i-0123456789abcdef0"
            ),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"
            },
            region="us-east-1",
            service="ec2",
            credentials=AwsCredentials(
                "AKIDEXAMPLE",
                "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                "SESSIONTOKEN",
                datetime(2026, 11, 9, 13, tzinfo=UTC),
            ),
            now=datetime(2026, 11, 9, 12, tzinfo=UTC),
        )

        self.assertEqual(headers["X-Amz-Date"], "20261109T120000Z")
        self.assertEqual(headers["X-Amz-Security-Token"], "SESSIONTOKEN")
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261109/us-east-1/ec2/"
            "aws4_request, SignedHeaders=content-type;host;x-amz-date;"
            "x-amz-security-token, Signature="
            "51ff27bd0d64c3438ed38a5dbcc8a7ac8cf61092afa0bcd55bee6ad02b289e6e",
        )


class EC2PrimaryTests(unittest.TestCase):
    def test_rejects_malformed_identity_inputs(self) -> None:
        for override in (
            {"region": "us east"},
            {"instance_id": "web-1"},
            {"role_arn": "arn:aws:iam::1:role/x"},
            {"audience": ""},
        ):
            with self.subTest(override=override), self.assertRaises(AwsError):
                EC2Primary(
                    **{
                        "region": "us-east-1",
                        "instance_id": _INSTANCE,
                        "role_arn": _ROLE,
                        "audience": "ditto-screener-capacity",
                        **override,
                    }
                )

    def test_status_assumes_the_role_keylessly_and_signs_the_query(self) -> None:
        opener = _Urlopen([_describe_xml("stopped")])
        with patch("screener_capacity.aws.urllib.request.urlopen", opener):
            status = _primary().status()

        self.assertEqual(status.state, "stopped")
        self.assertEqual(status.launched_at, datetime(2026, 11, 9, 11, 58, tzinfo=UTC))
        metadata, sts, ec2 = opener.requests
        self.assertIn("audience=ditto-screener-capacity", metadata.full_url)
        sts_form = urllib.parse.parse_qs(sts.data.decode())
        self.assertEqual(sts_form["Action"], ["AssumeRoleWithWebIdentity"])
        self.assertEqual(sts_form["RoleArn"], [_ROLE])
        self.assertEqual(sts_form["WebIdentityToken"], [_ID_TOKEN])
        self.assertIsNone(sts.get_header("Authorization"))
        self.assertEqual(ec2.full_url, "https://ec2.us-east-1.amazonaws.com/")
        self.assertTrue(
            ec2.get_header("Authorization").startswith(
                "AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/"
            )
        )
        self.assertEqual(ec2.get_header("X-amz-security-token"), "session")

    def test_credentials_are_reused_until_near_expiry(self) -> None:
        opener = _Urlopen([_describe_xml("stopped"), _START_XML])
        primary = _primary()
        with patch("screener_capacity.aws.urllib.request.urlopen", opener):
            primary.status()
            primary.start()

        self.assertEqual(opener.sts_calls, 1)
        start_form = urllib.parse.parse_qs(opener.requests[-1].data.decode())
        self.assertEqual(start_form["Action"], ["StartInstances"])
        self.assertEqual(start_form["InstanceId.1"], [_INSTANCE])

    def test_unexpected_responses_fail_closed(self) -> None:
        for body in (_describe_xml("rebooting"), _describe_xml("stopped", "i-0fff")):
            with (
                self.subTest(body=body[-120:]),
                patch("screener_capacity.aws.urllib.request.urlopen", _Urlopen([body])),
                self.assertRaises(AwsError),
            ):
                _primary().status()

    def test_http_errors_are_redacted(self) -> None:
        def refuse(request: Any, timeout: float) -> _Response:
            raise urllib.error.HTTPError(
                request.full_url, 403, "AccessDenied secret-detail", None, None
            )

        with (
            patch("screener_capacity.aws.urllib.request.urlopen", refuse),
            self.assertRaises(AwsError) as raised,
        ):
            _primary().start()
        self.assertNotIn("secret-detail", str(raised.exception))
