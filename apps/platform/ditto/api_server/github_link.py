"""Server-side GitHub code exchange; access tokens are transient."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import TypeAdapter

from ditto.api_server.ditto_link import code_challenge_s256
from ditto_screening_protocol.contributor_identity import GithubLogin

ATTEMPT_TTL_SECONDS = 600


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


class GithubLinkClient:
    def __init__(
        self,
        config: GithubLinkConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return self.config.enabled

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
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as client:
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
                if (
                    not isinstance(token, str)
                    or not token
                    or token_response.get("token_type", "").lower() != "bearer"
                ):
                    raise GithubLinkRejected("GitHub did not issue an identity token")
                if token_response.get("scope", "").strip():
                    raise GithubLinkRejected(
                        "GitHub linking requires a public-profile-only OAuth app"
                    )
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
                if type(user_id) is not int or not 0 < user_id <= 9223372036854775807:
                    raise GithubLinkRejected("GitHub returned an invalid account id")
                login = TypeAdapter(GithubLogin).validate_python(login)
                return GithubIdentity(user_id=user_id, login=login)
        except GithubLinkRejected:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Provider exceptions and bodies can carry secrets. Discard them.
            raise GithubLinkRejected(
                "GitHub identity verification failed; start a new link attempt"
            ) from None

    async def public_identity(self, login: str) -> GithubIdentity:
        """Revalidate a recorded display label before accepting a bounty claim.

        Numeric id must still match the immutable OAuth link. The server-only
        app credentials use its own public-data rate budget; no miner token or
        stale identity cache is used. Provider failures never prove identity.
        """
        if not self.config.client_id or not self.config.client_secret:
            raise GithubLinkRejected(
                "live GitHub identity verification requires configured "
                "OAuth app credentials"
            )
        try:
            login = TypeAdapter(GithubLogin).validate_python(login)
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(
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
                if response.is_redirect:
                    raise GithubLinkRejected(
                        "GitHub login has moved; re-link before claiming"
                    )
                response.raise_for_status()
                user = response.json()
                user_id, current_login = user.get("id"), user.get("login")
                if type(user_id) is not int or not 0 < user_id <= 9223372036854775807:
                    raise GithubLinkRejected("GitHub account identity is unavailable")
                return GithubIdentity(
                    user_id, TypeAdapter(GithubLogin).validate_python(current_login)
                )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise GithubLinkRejected(
                "GitHub account identity is unavailable; retry before claiming"
            ) from None
