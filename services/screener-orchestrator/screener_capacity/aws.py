"""Keyless start and inspection of the on-demand AWS EC2 screener primary.

The controller VM's Google service account proves itself to AWS with a
Google-signed ID token (``sts:AssumeRoleWithWebIdentity``), so no AWS key
exists anywhere. The assumed role may only describe instances and start the one
screener instance. The instance stops itself when its workers go idle, so this
client never stops, terminates, or modifies it.

Stdlib only: STS web-identity exchange is unsigned, and the two EC2 Query API
calls are signed here with AWS Signature Version 4.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)
_STS_VERSION = "2011-06-15"
_EC2_VERSION = "2016-11-15"
_INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
# Refresh temporary credentials this long before AWS expires them.
_CREDENTIAL_MARGIN = timedelta(minutes=5)

InstanceState = Literal[
    "pending", "running", "shutting-down", "terminated", "stopping", "stopped"
]
_STATES: frozenset[str] = frozenset(
    {"pending", "running", "shutting-down", "terminated", "stopping", "stopped"}
)


class AwsError(RuntimeError):
    """A redacted AWS failure; never carries tokens, keys, or signatures."""


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: datetime


@dataclass(frozen=True)
class InstanceStatus:
    state: InstanceState
    launched_at: datetime | None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    host: str,
    path: str,
    query: str,
    body: bytes,
    headers: dict[str, str],
    region: str,
    service: str,
    credentials: AwsCredentials,
    now: datetime,
) -> dict[str, str]:
    """Return ``headers`` plus the SigV4 ``Authorization`` and date headers.

    Signs every supplied header and ``host``. ``query`` must already be in
    canonical form (sorted, URI-encoded) or empty.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    signed = {key.lower(): " ".join(value.split()) for key, value in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        [
            method,
            path,
            query,
            canonical_headers,
            signed_headers,
            _sha256_hex(body),
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            _sha256_hex(canonical_request.encode()),
        ]
    )
    key = _hmac(f"AWS4{credentials.secret_access_key}".encode(), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = dict(headers)
    result["X-Amz-Date"] = amz_date
    if credentials.session_token:
        result["X-Amz-Security-Token"] = credentials.session_token
    result["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ElementTree.Element, name: str) -> str | None:
    for element in root.iter():
        if _local(element.tag) == name and element.text is not None:
            return element.text.strip()
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _post(url: str, body: bytes, headers: dict[str, str], *, what: str) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload: bytes = response.read()
            return payload
    except urllib.error.HTTPError as error:
        # The body may echo request details; report only the status.
        raise AwsError(f"{what} failed with HTTP {error.code}") from None
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        raise AwsError(f"{what} transport failed") from error


class EC2Primary:
    """Describe and start one EC2 instance through a web-identity role."""

    def __init__(
        self,
        *,
        region: str,
        instance_id: str,
        role_arn: str,
        audience: str,
        session_name: str = "ditto-screener-capacity",
    ) -> None:
        if _REGION_RE.fullmatch(region) is None:
            raise AwsError("AWS region is invalid")
        if _INSTANCE_ID_RE.fullmatch(instance_id) is None:
            raise AwsError("AWS primary instance id is invalid")
        if _ROLE_ARN_RE.fullmatch(role_arn) is None:
            raise AwsError("AWS primary role ARN is invalid")
        if not audience or len(audience) > 256:
            raise AwsError("AWS web-identity audience is invalid")
        self.region = region
        self.instance_id = instance_id
        self.role_arn = role_arn
        self.audience = audience
        self.session_name = session_name
        self._credentials: AwsCredentials | None = None

    def _google_id_token(self) -> str:
        url = f"{_METADATA_IDENTITY_URL}?" + urllib.parse.urlencode(
            {"audience": self.audience, "format": "full"}
        )
        request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                token = str(response.read().decode()).strip()
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            raise AwsError("Google identity token is unavailable") from error
        if token.count(".") != 2:
            raise AwsError("Google identity token is invalid")
        return token

    def _assume_role(self) -> AwsCredentials:
        body = urllib.parse.urlencode(
            {
                "Action": "AssumeRoleWithWebIdentity",
                "Version": _STS_VERSION,
                "RoleArn": self.role_arn,
                "RoleSessionName": self.session_name,
                "WebIdentityToken": self._google_id_token(),
                "DurationSeconds": "900",
            }
        ).encode()
        payload = _post(
            f"https://sts.{self.region}.amazonaws.com/",
            body,
            {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            what="AWS web-identity role assumption",
        )
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as error:
            raise AwsError("AWS STS response is invalid") from error
        access_key = _find_text(root, "AccessKeyId")
        secret_key = _find_text(root, "SecretAccessKey")
        session_token = _find_text(root, "SessionToken")
        expires_at = _parse_time(_find_text(root, "Expiration"))
        if not access_key or not secret_key or not session_token or not expires_at:
            raise AwsError("AWS STS response is missing credentials")
        return AwsCredentials(access_key, secret_key, session_token, expires_at)

    def _current_credentials(self) -> AwsCredentials:
        now = datetime.now(UTC)
        if (
            self._credentials is None
            or self._credentials.expires_at - _CREDENTIAL_MARGIN <= now
        ):
            self._credentials = self._assume_role()
        return self._credentials

    def _ec2(self, action: str, parameters: dict[str, str]) -> ElementTree.Element:
        host = f"ec2.{self.region}.amazonaws.com"
        body = urllib.parse.urlencode(
            {"Action": action, "Version": _EC2_VERSION, **parameters}
        ).encode()
        headers = sigv4_headers(
            method="POST",
            host=host,
            path="/",
            query="",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"
            },
            region=self.region,
            service="ec2",
            credentials=self._current_credentials(),
            now=datetime.now(UTC),
        )
        payload = _post(f"https://{host}/", body, headers, what=f"EC2 {action}")
        try:
            return ElementTree.fromstring(payload)
        except ElementTree.ParseError as error:
            raise AwsError(f"EC2 {action} response is invalid") from error

    def status(self) -> InstanceStatus:
        root = self._ec2("DescribeInstances", {"InstanceId.1": self.instance_id})
        for element in root.iter():
            if _local(element.tag) != "instancesSet":
                continue
            for item in element:
                if _find_text(item, "instanceId") != self.instance_id:
                    continue
                state = None
                for child in item:
                    if _local(child.tag) == "instanceState":
                        state = _find_text(child, "name")
                if state not in _STATES:
                    raise AwsError("EC2 instance state is invalid")
                return InstanceStatus(
                    state=state,  # type: ignore[arg-type]
                    launched_at=_parse_time(_find_text(item, "launchTime")),
                )
        raise AwsError("EC2 primary instance was not found")

    def start(self) -> None:
        root = self._ec2("StartInstances", {"InstanceId.1": self.instance_id})
        if _find_text(root, "instanceId") != self.instance_id:
            raise AwsError("EC2 StartInstances response is invalid")
