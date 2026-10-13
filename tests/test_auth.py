"""Auth tests against a local fake of Google's issue_token and Nest's issue_jwt.

All values are synthetic.
"""

import pytest
from aiohttp import DummyCookieJar, web
from aiohttp.test_utils import TestServer
import aiohttp

from pynestproto.auth import (
    AuthUnavailable,
    CredentialsRejected,
    GoogleCredentials,
    NestAuth,
    merge_set_cookies,
    parse_capture,
)

ISSUE_URL = (
    "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken"
    "&response_type=token%20id_token&client_id=abc.apps.googleusercontent.com"
)
COOKIES = "SID=sid1; HSID=h1; SIDCC=cc1; __Secure-3PSIDCC=x1"


# --- parse_capture ------------------------------------------------------------

FIREFOX_CURL = f"""curl '{ISSUE_URL}' \\
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0' \\
  -H 'Accept: */*' \\
  -H 'X-Requested-With: XmlHttpRequest' \\
  -H 'Connection: keep-alive' \\
  -H 'Referer: https://accounts.google.com/o/oauth2/iframe' \\
  -H 'Cookie: {COOKIES}' \\
  -H 'Sec-Fetch-Mode: cors'"""


def test_parse_firefox_curl():
    creds = parse_capture(FIREFOX_CURL)
    assert creds.issue_token == ISSUE_URL
    assert creds.cookies == COOKIES


def test_parse_curl_with_cookie_flag():
    creds = parse_capture(f"curl '{ISSUE_URL}' -b '{COOKIES}' -H 'Accept: */*'")
    assert creds.cookies == COOKIES


@pytest.mark.parametrize(
    "text",
    [
        f"{ISSUE_URL}\n{COOKIES}",
        f"{COOKIES}\n{ISSUE_URL}",
        f"'{ISSUE_URL}'\n\nCookie: {COOKIES}\n",
    ],
)
def test_parse_two_lines_any_order(text):
    creds = parse_capture(text)
    assert (creds.issue_token, creds.cookies) == (ISSUE_URL, COOKIES)


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        (COOKIES, "No issue token URL"),
        (f"https://home.nest.com/session\n{COOKIES}", "not the issue token request"),
        (ISSUE_URL, "No cookies"),
        (f"{ISSUE_URL}\nHSID=h1; SIDCC=cc1", "no Google session cookie"),
    ],
)
def test_parse_rejects_bad_input(text, complaint):
    with pytest.raises(ValueError, match=complaint):
        parse_capture(text)


def test_credentials_repr_is_redacted():
    assert "sid1" not in repr(parse_capture(f"{ISSUE_URL}\n{COOKIES}"))


# --- merge_set_cookies --------------------------------------------------------


def test_merge_rotates_and_reports_changes():
    merged, changed = merge_set_cookies(
        COOKIES,
        [
            "SIDCC=cc2; expires=Fri, 01-Jan-2100 00:00:00 GMT; path=/; domain=.google.com; priority=high",
            "SID=sid1; path=/",  # unchanged value
        ],
    )
    assert changed == ["SIDCC"]
    assert merged == "SID=sid1; HSID=h1; SIDCC=cc2; __Secure-3PSIDCC=x1"


def test_merge_deletes_expired_cookies():
    merged, changed = merge_set_cookies(COOKIES, ["HSID=; Max-Age=0; path=/"])
    assert changed == ["HSID"]
    assert "HSID" not in merged


def test_merge_without_changes_returns_original():
    assert merge_set_cookies(COOKIES, []) == (COOKIES, [])


# --- NestAuth against a fake server --------------------------------------------


class FakeServer:
    def __init__(self) -> None:
        self.issue_calls = 0
        self.jwt_calls = 0
        self.issue_status = 200
        self.issue_body: dict = {"access_token": "ya29.fake", "expires_in": 3599}
        self.seen_cookies: list[str] = []
        self.seen_jwt_form: dict = {}
        self.seen_jwt_auth = ""

    async def issue_token(self, request: web.Request) -> web.Response:
        self.issue_calls += 1
        self.seen_cookies.append(request.headers.get("Cookie", ""))
        resp = web.json_response(self.issue_body, status=self.issue_status)
        resp.headers.add("Set-Cookie", f"SIDCC=cc{self.issue_calls + 1}; path=/; priority=high")
        return resp

    async def issue_jwt(self, request: web.Request) -> web.Response:
        self.jwt_calls += 1
        self.seen_jwt_form = dict(await request.post())
        self.seen_jwt_auth = request.headers.get("Authorization", "")
        return web.json_response(
            {
                "jwt": f"g.0.fakejwt{self.jwt_calls}",
                "claims": {
                    "subject": {"nestId": {"namespaceId": {"id": "nest-phoenix-prod"}, "id": "12345"}},
                    "expirationTime": "2100-01-01T00:00:00.000Z",
                    "policyId": "authproxy-oauth-policy",
                },
            }
        )


@pytest.fixture
async def fake():
    state = FakeServer()
    app = web.Application()
    app.router.add_get("/o/oauth2/iframerpc", state.issue_token)
    app.router.add_post("/v1/issue_jwt", state.issue_jwt)
    server = TestServer(app)
    await server.start_server()
    state.base = str(server.make_url(""))
    yield state
    await server.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession(cookie_jar=DummyCookieJar()) as s:
        yield s


def make_auth(fake, session, saved):
    creds = GoogleCredentials(
        issue_token=f"{fake.base}/o/oauth2/iframerpc?action=issueToken", cookies=COOKIES
    )
    return NestAuth(
        session, creds, on_cookies_rotated=saved.append, issue_jwt_url=f"{fake.base}/v1/issue_jwt"
    )


@pytest.mark.asyncio
async def test_mints_token_and_persists_rotated_cookies(fake, session):
    saved = []
    auth = make_auth(fake, session, saved)
    token = await auth.async_get_token()

    assert token.user_id == "12345"
    assert token.authorization == "Basic g.0.fakejwt1"
    assert fake.seen_cookies == [COOKIES]
    assert fake.seen_jwt_auth == "Bearer ya29.fake"
    assert fake.seen_jwt_form["google_oauth_access_token"] == "ya29.fake"
    assert fake.seen_jwt_form["policy_id"] == "authproxy-oauth-policy"
    # Rotation was reported and is what the next request will use.
    assert [c.cookies for c in saved] == ["SID=sid1; HSID=h1; SIDCC=cc2; __Secure-3PSIDCC=x1"]
    assert auth.credentials == saved[-1]


@pytest.mark.asyncio
async def test_token_is_cached_until_invalidated(fake, session):
    saved = []
    auth = make_auth(fake, session, saved)
    await auth.async_get_token()
    await auth.async_get_token()
    assert fake.issue_calls == 1

    auth.invalidate()
    token = await auth.async_get_token()
    assert fake.issue_calls == 2
    assert token.jwt == "g.0.fakejwt2"
    # The second request sent the cookies rotated by the first.
    assert "SIDCC=cc2" in fake.seen_cookies[1]


@pytest.mark.asyncio
async def test_google_rejection_still_persists_rotation(fake, session):
    fake.issue_body = {"error": "USER_LOGGED_OUT", "detail": "No active session found."}
    saved = []
    auth = make_auth(fake, session, saved)
    with pytest.raises(CredentialsRejected, match="No active session"):
        await auth.async_get_token()
    assert len(saved) == 1
    assert fake.jwt_calls == 0


@pytest.mark.asyncio
async def test_server_error_is_transient(fake, session):
    fake.issue_status = 503
    with pytest.raises(AuthUnavailable):
        await make_auth(fake, session, []).async_get_token()


# --- Sharing tokens and cookies between processes ------------------------------

from datetime import UTC, datetime, timedelta

from pynestproto.auth import NestToken


def shared_token(jwt="g.0.shared", minutes=30):
    return NestToken(jwt=jwt, user_id="12345", expires_at=datetime.now(UTC) + timedelta(minutes=minutes))


async def test_reuses_token_another_process_minted(fake, session):
    creds = GoogleCredentials(issue_token=f"{fake.base}/o/oauth2/iframerpc?action=issueToken", cookies=COOKIES)
    auth = NestAuth(session, creds, reload=lambda: (creds, shared_token()), issue_jwt_url=f"{fake.base}/v1/issue_jwt")
    token = await auth.async_get_token()
    assert token.jwt == "g.0.shared"
    assert fake.issue_calls == 0  # Google never touched, so no cookie rotation


async def test_rejected_shared_token_is_not_reused(fake, session):
    creds = GoogleCredentials(issue_token=f"{fake.base}/o/oauth2/iframerpc?action=issueToken", cookies=COOKIES)
    minted = []
    auth = NestAuth(
        session, creds,
        reload=lambda: (creds, shared_token()),
        on_token=minted.append,
        issue_jwt_url=f"{fake.base}/v1/issue_jwt",
    )
    await auth.async_get_token()
    auth.invalidate()  # the server rejected g.0.shared
    token = await auth.async_get_token()
    assert token.jwt == "g.0.fakejwt1"
    assert minted == [token]  # published for the other processes


async def test_refresh_uses_newest_cookies_from_disk(fake, session):
    stale = GoogleCredentials(issue_token=f"{fake.base}/o/oauth2/iframerpc?action=issueToken", cookies=COOKIES)
    fresh = GoogleCredentials(issue_token=stale.issue_token, cookies="SID=sid1; SIDCC=from-other-process")
    auth = NestAuth(session, stale, reload=lambda: (fresh, None), issue_jwt_url=f"{fake.base}/v1/issue_jwt")
    await auth.async_get_token()
    assert fake.seen_cookies == ["SID=sid1; SIDCC=from-other-process"]


async def test_expiring_shared_token_is_refreshed(fake, session):
    creds = GoogleCredentials(issue_token=f"{fake.base}/o/oauth2/iframerpc?action=issueToken", cookies=COOKIES)
    auth = NestAuth(session, creds, reload=lambda: (creds, shared_token(minutes=2)), issue_jwt_url=f"{fake.base}/v1/issue_jwt")
    assert (await auth.async_get_token()).jwt == "g.0.fakejwt1"  # 2 min left < 5 min margin
