"""Authentication for the home.nest.com protobuf API.

Credentials are a Google sign-in session captured from a browser: the URL of
the ``iframerpc?action=issueToken`` request and the cookies sent with it.

    cookies + issue_token URL  --GET-->   Google OAuth access token (ya29...)
    access token               --POST-->  nestauthproxyservice issue_jwt
                                          -> Nest JWT, sent as "Basic <jwt>"

Google rotates some of the cookies (SIDCC and friends) on every issue_token
request and stops accepting superseded values after a rotation or two, so
whoever stores the credentials must save the rotated cookie string each time.
``NestAuth`` reports it through the ``on_cookies_rotated`` callback.

Use an aiohttp session with ``aiohttp.DummyCookieJar()``: we send the cookie
header explicitly, and a real jar would add its own copies of the same cookies.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp

ISSUE_TOKEN_PREFIX = "https://accounts.google.com/o/oauth2/iframerpc"
ISSUE_JWT_URL = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"

# At least one of these carries the Google sign-in session. A cookie string
# without any of them was copied from the wrong request or got truncated.
SESSION_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID"})

# Credentials are captured in Firefox (Chromium binds sessions to the device),
# so present as Firefox to Google.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class AuthError(Exception):
    """Authentication failed."""


class CredentialsRejected(AuthError):
    """Google or Nest rejected the credentials. A fresh browser capture is needed."""


class AuthUnavailable(AuthError):
    """A transient failure (network, 5xx, rate limit). Retry later."""


@dataclass(frozen=True)
class GoogleCredentials:
    """A captured Google sign-in session."""

    issue_token: str
    cookies: str

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return "GoogleCredentials(<redacted>)"


@dataclass(frozen=True)
class NestToken:
    """A Nest JWT for the protobuf API."""

    jwt: str
    user_id: str
    expires_at: datetime

    def expires_within(self, seconds: float) -> bool:
        return datetime.now(UTC) + timedelta(seconds=seconds) >= self.expires_at

    @property
    def authorization(self) -> str:
        """Value for the Authorization header ("Basic" + raw JWT, by design)."""
        return f"Basic {self.jwt}"

    def __repr__(self) -> str:
        return f"NestToken(user_id={self.user_id!r}, expires_at={self.expires_at.isoformat()})"


# --- Parsing pasted credentials ---------------------------------------------


def parse_capture(text: str) -> GoogleCredentials:
    """Extract credentials from what a user pasted.

    Accepts Firefox's "Copy as cURL" of the iframerpc request, or the issue
    token URL and the cookie string on separate lines in either order (with or
    without a leading "Cookie:"). Raises ValueError explaining what's wrong.
    """
    text = text.strip()
    if text.startswith("curl"):
        issue_token, cookies = _parse_curl(text)
    else:
        issue_token, cookies = _parse_lines(text)

    if not issue_token:
        raise ValueError(
            "No issue token URL found. Copy the 'iframerpc' request "
            "(its URL starts with " + ISSUE_TOKEN_PREFIX + ")."
        )
    if not issue_token.startswith(ISSUE_TOKEN_PREFIX) or "action=issueToken" not in issue_token:
        raise ValueError(
            "That URL is not the issue token request. It must start with "
            f"{ISSUE_TOKEN_PREFIX} and contain action=issueToken."
        )
    if not cookies:
        raise ValueError("No cookies found. Copy the Cookie header of the iframerpc request.")
    names = set(_cookie_jar(cookies))
    if names.isdisjoint(SESSION_COOKIES):
        raise ValueError(
            "The cookies contain no Google session cookie (SID, __Secure-1PSID or "
            "__Secure-3PSID). They were probably cut short or copied from a different request."
        )
    return GoogleCredentials(issue_token=issue_token, cookies=cookies)


def _parse_curl(text: str) -> tuple[str | None, str | None]:
    # Join shell line continuations before tokenizing.
    tokens = shlex.split(re.sub(r"\\\r?\n", " ", text))
    issue_token = cookies = None
    it = iter(tokens[1:])
    for tok in it:
        if tok in ("-H", "--header"):
            header = next(it, "")
            name, _, value = header.partition(":")
            if name.strip().lower() == "cookie":
                cookies = value.strip()
        elif tok in ("-b", "--cookie"):
            cookies = next(it, "").strip()
        elif tok.startswith("http") and issue_token is None:
            issue_token = tok
    return issue_token, cookies


def _parse_lines(text: str) -> tuple[str | None, str | None]:
    issue_token = cookies = None
    for line in (ln.strip().strip("'\"") for ln in text.splitlines()):
        if not line:
            continue
        if line.startswith("http"):
            issue_token = line
        else:
            cookies = re.sub(r"^cookie:\s*", "", line, flags=re.IGNORECASE)
    return issue_token, cookies


# --- Cookie rotation ----------------------------------------------------------


def _cookie_jar(cookies: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for pair in cookies.split(";"):
        name, sep, value = pair.strip().partition("=")
        if sep and name:
            jar[name] = value
    return jar


def _deletes_cookie(attributes: list[str]) -> bool:
    max_age = expires = None
    for attr in attributes:
        key, _, value = attr.strip().partition("=")
        if key.lower() == "max-age":
            max_age = value.strip()
        elif key.lower() == "expires":
            expires = value.strip()
    if max_age is not None:  # Max-Age wins over Expires (RFC 6265 5.3)
        try:
            return int(max_age) <= 0
        except ValueError:
            pass
    if expires:
        try:
            when = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when <= datetime.now(UTC)
    return False


def merge_set_cookies(cookies: str, set_cookie_headers: list[str]) -> tuple[str, list[str]]:
    """Apply Set-Cookie headers to a Cookie header value.

    Returns the updated cookie string and the names that changed. Only the
    leading name=value of a Set-Cookie header is the cookie; the rest are
    attributes (don't use http.cookies for this: it turns attributes it
    doesn't know, like Google's "priority=high", into bogus cookies).
    """
    jar = _cookie_jar(cookies)
    changed: list[str] = []
    for header in set_cookie_headers:
        pair, *attributes = header.split(";")
        name, sep, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            continue
        if _deletes_cookie(attributes):
            if jar.pop(name, None) is not None:
                changed.append(name)
        elif jar.get(name) != value:
            jar[name] = value
            changed.append(name)
    if not changed:
        return cookies, changed
    return "; ".join(f"{k}={v}" for k, v in jar.items()), changed


# --- Token exchange -------------------------------------------------------------


def _is_transient(status: int) -> bool:
    return status >= 500 or status in (408, 429)


class NestAuth:
    """Mints and refreshes Nest JWTs from Google credentials.

    When several processes share one set of credentials (the dev tools do),
    pass ``reload`` and ``on_token``: before minting, ``reload`` returns the
    latest stored credentials and token, so a token another process already
    minted is reused instead of hitting Google, and cookies another process
    rotated are not overwritten with stale ones. A single owner (the HA
    integration) needs neither.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: GoogleCredentials,
        on_cookies_rotated: Callable[[GoogleCredentials], None] | None = None,
        *,
        token: NestToken | None = None,
        on_token: Callable[[NestToken], None] | None = None,
        reload: Callable[[], tuple[GoogleCredentials, NestToken | None]] | None = None,
        issue_jwt_url: str = ISSUE_JWT_URL,
    ) -> None:
        self._session = session
        self._issue_jwt_url = issue_jwt_url
        self._credentials = credentials
        self._on_cookies_rotated = on_cookies_rotated
        self._on_token = on_token
        self._reload = reload
        self._token = token
        self._rejected_jwt: str | None = None
        self._lock = asyncio.Lock()

    @property
    def credentials(self) -> GoogleCredentials:
        """Current credentials, including any rotated cookies."""
        return self._credentials

    async def async_get_token(self, min_validity: float = 300) -> NestToken:
        """Return a token valid for at least ``min_validity`` seconds."""
        async with self._lock:
            if self._usable(self._token, min_validity):
                return self._token
            if self._reload is not None:
                self._credentials, shared = self._reload()
                if self._usable(shared, min_validity):
                    self._token = shared
                    return shared
            self._token = await self._async_mint()
            if self._on_token:
                self._on_token(self._token)
            return self._token

    def _usable(self, token: NestToken | None, min_validity: float) -> bool:
        return (
            token is not None
            and token.jwt != self._rejected_jwt
            and not token.expires_within(min_validity)
        )

    def invalidate(self) -> None:
        """Force a new token on the next request (e.g. after a 401 from the API).

        The rejected token is remembered so a shared copy of it isn't reused.
        """
        if self._token is not None:
            self._rejected_jwt = self._token.jwt
        self._token = None

    async def _async_mint(self) -> NestToken:
        access_token = await self._async_google_access_token()
        return await self._async_issue_jwt(access_token)

    async def _async_google_access_token(self) -> str:
        creds = self._credentials
        try:
            async with self._session.get(
                creds.issue_token,
                headers={
                    "User-Agent": USER_AGENT,
                    "Sec-Fetch-Mode": "cors",
                    "X-Requested-With": "XmlHttpRequest",
                    "Referer": "https://accounts.google.com/o/oauth2/iframe",
                    "Cookie": creds.cookies,
                },
                timeout=_TIMEOUT,
            ) as resp:
                # Save rotated cookies first: Google has rotated them whether or
                # not the rest of this request succeeds.
                cookies, rotated = merge_set_cookies(
                    creds.cookies, resp.headers.getall("Set-Cookie", [])
                )
                if rotated:
                    self._credentials = replace(creds, cookies=cookies)
                    if self._on_cookies_rotated:
                        self._on_cookies_rotated(self._credentials)
                if _is_transient(resp.status):
                    raise AuthUnavailable(f"Google issue_token: HTTP {resp.status}")
                try:
                    data = await resp.json(content_type=None)
                except ValueError as err:
                    raise CredentialsRejected(
                        f"Google issue_token: unexpected response (HTTP {resp.status})"
                    ) from err
        except aiohttp.ClientError as err:
            raise AuthUnavailable(f"Google issue_token: {err}") from err

        if "error" in data or "access_token" not in data:
            detail = data.get("detail") or data.get("error") or "no access_token"
            raise CredentialsRejected(f"Google rejected the session: {detail}")
        return data["access_token"]

    async def _async_issue_jwt(self, access_token: str) -> NestToken:
        try:
            async with self._session.post(
                self._issue_jwt_url,
                data={
                    "embed_google_oauth_access_token": "true",
                    "expire_after": "3600s",
                    "google_oauth_access_token": access_token,
                    "policy_id": "authproxy-oauth-policy",
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": USER_AGENT,
                    "Referer": "https://home.nest.com",
                },
                timeout=_TIMEOUT,
            ) as resp:
                if _is_transient(resp.status):
                    raise AuthUnavailable(f"issue_jwt: HTTP {resp.status}")
                if resp.status in (401, 403):
                    raise CredentialsRejected(f"issue_jwt: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise AuthUnavailable(f"issue_jwt: {err}") from err

        try:
            claims = data["claims"]
            return NestToken(
                jwt=data["jwt"],
                user_id=claims["subject"]["nestId"]["id"],
                expires_at=datetime.fromisoformat(claims["expirationTime"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise CredentialsRejected(f"issue_jwt: unexpected response {list(data)}") from err

    def __repr__(self) -> str:
        return f"NestAuth(token={self._token!r})"
