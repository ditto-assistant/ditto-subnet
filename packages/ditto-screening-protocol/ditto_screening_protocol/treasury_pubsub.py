"""Bounded keyless mailbox over restricted Google APIs. No desktop credentials."""

import base64
import json
import re
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 131072


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


class TreasuryMailbox:
    """Per-topic IAM is the authentication boundary; never accepts an arbitrary URL."""

    def __init__(self, *, project, topic, subscription):
        for value in (project, topic, subscription):
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]{5,62}", value
            ):
                raise ValueError("explicit bounded mailbox resource required")
        self.topic = f"projects/{project}/topics/{topic}"
        self.subscription = f"projects/{project}/subscriptions/{subscription}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect)
        self._cached_token = None
        self._token_lock = Lock()

    def _json(self, request, *, allow_empty=False):
        with self.opener.open(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("mailbox response exceeds bound")
        if not raw and allow_empty:
            return {}
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("mailbox object required")
        return body

    def _token(self):
        # Memory only, monotonic deadline, refresh before expiration. The lock
        # prevents concurrent to_thread calls from stampeding metadata.
        with self._token_lock:
            now = monotonic()
            if self._cached_token and now < self._cached_token[1]:
                return self._cached_token[0]
            self._cached_token = None
            info = self._json(
                Request(
                    "http://metadata.google.internal/computeMetadata/v1/instance/"
                    "service-accounts/default/token",
                    headers={"Metadata-Flavor": "Google"},
                )
            )
            token = info["access_token"]
            if not isinstance(token, str) or not token or len(token) > 16384:
                raise ValueError("bounded metadata access token required")
            lifetime = info.get("expires_in")
            # Unknown/short lifetimes retain the uncached behavior. Never
            # cache beyond one hour even if metadata returns a larger value.
            if type(lifetime) is int and lifetime > 60:
                self._cached_token = (token, now + min(lifetime, 3600) - 60)
            return token

    def _call(self, resource, method, body):
        token = self._token()
        raw = json.dumps(body).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("mailbox request exceeds bound")
        try:
            return self._json(
                Request(
                    f"https://pubsub.googleapis.com/v1/{resource}:{method}",
                    data=raw,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                ),
                # Pub/Sub acknowledge may return no response payload. Metadata,
                # publish and pull must still provide their bounded JSON object.
                allow_empty=method == "acknowledge",
            )
        except HTTPError as error:
            if error.code == 401:
                with self._token_lock:
                    if self._cached_token and self._cached_token[0] == token:
                        self._cached_token = None
            # Never automatically retry publish: its delivery may be unknown.
            raise

    def publish(self, body):
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > 65536:
            raise ValueError("manual message exceeds bound")
        result = self._call(
            self.topic,
            "publish",
            {
                "messages": [{"data": base64.b64encode(raw).decode()}],
            },
        )
        if len(result.get("messageIds", [])) != 1:
            raise ValueError("mailbox publish acknowledgment absent")

    def pull(self):
        result = self._call(self.subscription, "pull", {"maxMessages": 1})
        rows = result.get("receivedMessages", [])
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValueError("bounded mailbox pull required")
        if not rows:
            return None
        row = rows[0]
        try:
            raw = base64.b64decode(row["message"]["data"], validate=True)
            if len(raw) > 65536:
                raise ValueError("manual message exceeds bound")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("manual message object required")
        except (ValueError, TypeError, KeyError) as error:
            # Undecodable bytes cannot be a valid financial request/report.
            # Keep ACK transport failures visible; never ACK a valid object
            # here before the consumer commits its durable processing.
            print(
                json.dumps(
                    {
                        "status": "mailbox_message_dropped",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
            self.ack(row["ackId"])
            return None
        return row["ackId"], body

    def ack(self, ack_id):
        self._call(self.subscription, "acknowledge", {"ackIds": [ack_id]})
