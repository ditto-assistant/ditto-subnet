"""Server-side GitHub code exchange; access tokens are transient."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import TypeAdapter

from ditto.api_server.ditto_link import code_challenge_s256
from ditto_screening_protocol.contributor_identity import GithubLogin

logger = logging.getLogger(__name__)
ATTEMPT_TTL_SECONDS = 600
# A consumed callback must finish its provider exchange within this window.
# httpx timeouts bound each connect/read/write separately, not a request, so
# the exchange enforces its own total deadlines that fit inside the window.
EXCHANGE_WINDOW_SECONDS = 180
EXCHANGE_DEADLINE_SECONDS = 150
TOKEN_REVOCATION_DEADLINE_SECONDS = 15
# GitHub answers a failed code exchange with 200 and an error code. Only these
# are about the miner's own sign-in (an expired or reused code, an unverified
# account); every other refusal is the app's configuration or GitHub's fault.
_SIGN_IN_TOKEN_ERRORS = frozenset({"bad_verification_code", "unverified_user_email"})
# One public identity lookup, connection to body, before a bounty claim.
PUBLIC_IDENTITY_DEADLINE_SECONDS = 30
# Backroom and the dashboard read account ids as JavaScript numbers, so a
# larger id could be stored but never read back exactly.
MAX_GITHUB_USER_ID = 2**53 - 1


@dataclass(frozen=True)
class GithubLinkConfig:
    enabled: bool = False
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    redirect_url: str = ""
    timeout_seconds: float = 10.0

    @property
    def origin(self) -> str:
        parts = urlsplit(self.redirect_url)
        host = parts.hostname or ""
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        if parts.port is not None and parts.port != {"http": 80, "https": 443}.get(
            parts.scheme
        ):
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}"


@dataclass(frozen=True)
class GithubIdentity:
    user_id: int
    login: str


class GithubLinkRejected(Exception):
    pass


class GithubProviderUnavailable(GithubLinkRejected):
    """Platform could not get a usable answer from GitHub.

    The app has no credentials, or GitHub failed: network, timeout, an error
    status, a malformed body. A provider fault to retry, never a decision by
    the miner or a verdict on an identity.
    """


class GithubLinkClient:
    def __init__(
        self,
        config: GithubLinkConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self._public: httpx.AsyncClient | None = None
        # Revocations the client owns until they finish. A cancelled request
        # stops waiting for its revocation, but the event loop keeps only weak
        # references to tasks, so without this one could be collected before
        # the token is revoked.
        self._revocations: set[asyncio.Task[bool]] = set()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _public_client(self) -> httpx.AsyncClient:
        """One pooled client for app-authenticated public lookups.

        Only server credentials pass through it, and it keeps no cookies, so a
        lookup never carries state from another miner's. OAuth exchanges hold
        a miner's token and keep their own short-lived clients.
        """
        if self._public is None or self._public.is_closed:
            self._public = httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
                cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])),
            )
        return self._public

    async def aclose(self) -> None:
        # Pending revocations finish first; each has its own deadline.
        if self._revocations:
            await asyncio.gather(*self._revocations, return_exceptions=True)
        if self._public is not None:
            await self._public.aclose()

    def _start_revocation(self, token: str) -> asyncio.Task[bool]:
        task = asyncio.ensure_future(self._revoke_token(token))
        self._revocations.add(task)
        task.add_done_callback(self._revocations.discard)
        return task

    def authorize_url(self, *, state: str, verifier: str) -> str:
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_url,
                "scope": "",
                "state": state,
                "code_challenge": code_challenge_s256(verifier),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}"

    async def exchange(self, *, code: str, verifier: str) -> GithubIdentity:
        # The deadline bounds GitHub's two answers, not the revocation after
        # them, which has its own bound: a deadline reached while revoking
        # must not replace the verification's own outcome.
        deadline = asyncio.get_running_loop().time() + EXCHANGE_DEADLINE_SECONDS
        try:
            return await self._exchange(code=code, verifier=verifier, deadline=deadline)
        except TimeoutError:
            raise GithubProviderUnavailable(
                "GitHub did not answer in time; start a new link attempt"
            ) from None

    async def _exchange(
        self, *, code: str, verifier: str, deadline: float
    ) -> GithubIdentity:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as client:
                async with asyncio.timeout_at(deadline):
                    response = await client.post(
                        "https://github.com/login/oauth/access_token",
                        headers={"Accept": "application/json"},
                        data={
                            "client_id": self.config.client_id,
                            "client_secret": self.config.client_secret,
                            "redirect_uri": self.config.redirect_url,
                            "code": code,
                            "code_verifier": verifier,
                        },
                    )
                response.raise_for_status()
                token_response = response.json()
                token = token_response.get("access_token")
                if not isinstance(token, str) or not token:
                    if token_response.get("error") in _SIGN_IN_TOKEN_ERRORS:
                        raise GithubLinkRejected(
                            "GitHub did not accept this sign-in; start a new "
                            "link attempt"
                        )
                    raise GithubProviderUnavailable(
                        "GitHub did not issue an identity token"
                    )
                verified = False
                try:
                    if token_response.get("token_type", "").lower() != "bearer":
                        # A malformed token response, not the miner's answer.
                        raise GithubProviderUnavailable(
                            "GitHub did not issue an identity token"
                        )
                    # Extra scopes stay a rejection: the authorize URL passes
                    # through the miner's browser, which can widen the scope.
                    if token_response.get("scope", "").strip():
                        raise GithubLinkRejected(
                            "GitHub linking requires a public-profile-only OAuth app"
                        )
                    async with asyncio.timeout_at(deadline):
                        user_response = await client.get(
                            "https://api.github.com/user",
                            headers={
                                "Accept": "application/vnd.github+json",
                                "Authorization": f"Bearer {token}",
                                "X-GitHub-Api-Version": "2022-11-28",
                            },
                        )
                    user_response.raise_for_status()
                    user = user_response.json()
                    user_id, login = user.get("id"), user.get("login")
                    if (
                        type(user_id) is not int
                        or not 0 < user_id <= MAX_GITHUB_USER_ID
                    ):
                        # A malformed body, like an invalid login below.
                        raise GithubProviderUnavailable(
                            "GitHub returned an invalid account id"
                        )
                    login = TypeAdapter(GithubLogin).validate_python(login)
                    identity = GithubIdentity(user_id=user_id, login=login)
                    verified = True
                    return identity
                finally:
                    # Revoke the single transient token on success, rejection,
                    # deadline or cancellation. The revocation runs as its own
                    # shielded task with its own client and bound, owned by
                    # this client, so a cancelled request still revokes it
                    # (and logs a failure).
                    cleanup_ok = await asyncio.shield(self._start_revocation(token))
                    # Preserve the original rejection. Cleanup cannot turn a
                    # failed verification into success or expose provider errors.
                    if verified and not cleanup_ok:
                        raise GithubProviderUnavailable(
                            "GitHub token cleanup failed; start a new link attempt"
                        ) from None
        except GithubLinkRejected:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Provider exceptions and bodies can carry secrets. Discard them.
            raise GithubProviderUnavailable(
                "GitHub identity verification failed; start a new link attempt"
            ) from None

    async def _revoke_token(self, token: str) -> bool:
        """Revoke one transient token. Never the user's other tokens or grant."""
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as client:
                revoked = await asyncio.wait_for(
                    client.request(
                        "DELETE",
                        f"https://api.github.com/applications/{self.config.client_id}/token",
                        auth=httpx.BasicAuth(
                            self.config.client_id or "",
                            self.config.client_secret or "",
                        ),
                        headers={
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        json={"access_token": token},
                    ),
                    TOKEN_REVOCATION_DEADLINE_SECONDS,
                )
            if revoked.status_code == 204:
                return True
            failure = f"status={revoked.status_code}"
        except Exception as exc:
            failure = f"error_type={type(exc).__name__}"
        # A rejection hides this from the miner, and the token stays valid, so
        # operators need the signal. Never log the token or the response body.
        logger.warning("GitHub transient token revocation failed: %s", failure)
        return False

    async def public_identity(self, login: str) -> GithubIdentity:
        """Revalidate a recorded display label before accepting a bounty claim.

        Numeric id must still match the immutable OAuth link. The server-only
        app credentials use its own public-data rate budget; no miner token or
        stale identity cache is used. Provider failures never prove identity:
        ``GithubLinkRejected`` means the account itself is gone or moved, and
        ``GithubProviderUnavailable`` means GitHub could not answer usably.
        """
        if not self.config.client_id or not self.config.client_secret:
            raise GithubProviderUnavailable(
                "live GitHub identity verification requires configured "
                "OAuth app credentials"
            )
        try:
            login = TypeAdapter(GithubLogin).validate_python(login)
        except ValueError:
            # The recorded label itself is unusable; GitHub was never asked.
            raise GithubLinkRejected(
                "GitHub account identity is unavailable; re-link before claiming"
            ) from None
        try:
            async with asyncio.timeout(PUBLIC_IDENTITY_DEADLINE_SECONDS):
                response = await self._public_client().get(
                    f"https://api.github.com/users/{login}",
                    # Documented OAuth app authentication for public data:
                    # https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-oauth-apps
                    auth=httpx.BasicAuth(
                        self.config.client_id, self.config.client_secret
                    ),
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            if response.status_code == 404:
                raise GithubLinkRejected(
                    "GitHub account was not found; re-link before claiming"
                )
            # Only GitHub's permanent rename redirect says the account moved.
            # A temporary redirect (302, 307) means retry the same URL later,
            # so it and any other 3xx are provider faults, like an error.
            if response.status_code in (301, 308) and response.has_redirect_location:
                raise GithubLinkRejected(
                    "GitHub login has moved; re-link before claiming"
                )
            # Rate limits, credential errors and outages are provider faults.
            response.raise_for_status()
            user = response.json()
            user_id, current_login = user.get("id"), user.get("login")
            if type(user_id) is not int or not 0 < user_id <= MAX_GITHUB_USER_ID:
                raise ValueError("invalid account id")
            return GithubIdentity(
                user_id, TypeAdapter(GithubLogin).validate_python(current_login)
            )
        except GithubLinkRejected:
            raise
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Provider exceptions and bodies can carry secrets. Discard them.
            raise GithubProviderUnavailable(
                "GitHub account identity is unavailable; retry before claiming"
            ) from None
