"""Signing in with Discord (OAuth2 authorisation code grant), and the sessions it opens.

The access token is used once, to learn who signed in and which servers they're in, then
revoked: nothing needs it again, so nothing stores it. Sessions live in memory only, like the
panel's interaction tokens. A restart signs everyone out, and the page signs back in through
Discord without asking again (`prompt=none`).
"""

import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

_log = logging.getLogger(__name__)

SESSION_COOKIE = "minie_session"
STATE_COOKIE = "minie_oauth_state"
SESSION_LIFETIME = 7 * 24 * 3600.0
STATE_LIFETIME = 600
SCOPES = "identify guilds"


@dataclass(frozen=True)
class Session:
    user_id: int
    name: str
    guild_ids: frozenset[int]
    """The servers they were in at sign-in, for the server picker only: every request checks
    membership again."""
    expires: float


class Sessions:
    """Signed-in browsers, keyed by a hash of their cookie so that lookups take the same time
    whatever a guessed cookie shares with a real one."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._sessions: dict[bytes, Session] = {}

    def create(self, user_id: int, name: str, guild_ids: frozenset[int]) -> str:
        now = self._clock()
        self._sessions = {k: s for k, s in self._sessions.items() if s.expires > now}
        token = secrets.token_urlsafe(32)  # 256 bits
        self._sessions[_key(token)] = Session(user_id, name, guild_ids, now + SESSION_LIFETIME)
        return token

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self._sessions.get(_key(token))
        if session is None or session.expires <= self._clock():
            return None
        return session

    def delete(self, token: str | None) -> None:
        if token:
            self._sessions.pop(_key(token), None)


def _key(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class DiscordUser:
    id: int
    name: str
    guild_ids: frozenset[int]


class OAuthError(Exception):
    pass


class DiscordOAuth:
    """The few OAuth2 calls the sign-in needs. The URLs can point at a fake server in tests."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        *,
        client_secret: str,
        redirect_uri: str,
        api: str = "https://discord.com/api/v10",
        authorize: str = "https://discord.com/oauth2/authorize",
    ) -> None:
        self._http = http
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._api = api
        self._authorize = authorize

    def authorize_url(self, client_id: int, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "scope": SCOPES,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "prompt": "none",
            }
        )
        return f"{self._authorize}?{query}"

    async def sign_in(self, client_id: int, code: str) -> DiscordUser:
        auth = {"Authorization": aiohttp.encode_basic_auth(str(client_id), self._client_secret)}
        token = await self._post(
            "/oauth2/token",
            auth,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
        )
        access_token = token.get("access_token")
        if not isinstance(access_token, str):
            raise OAuthError("Discord returned no access token")
        try:
            headers = {"Authorization": f"Bearer {access_token}"}
            user = await self._get("/users/@me", headers)
            # One page holds 200 servers; the bot only cares about those it shares.
            guilds = await self._get("/users/@me/guilds", headers)
        finally:
            await self._revoke(auth, access_token)
        if not isinstance(user, dict) or not isinstance(guilds, list):
            raise OAuthError("unexpected answer from Discord")
        return DiscordUser(
            id=int(user["id"]),
            name=str(user.get("global_name") or user["username"]),
            guild_ids=frozenset(int(guild["id"]) for guild in guilds),
        )

    async def _post(self, path: str, auth: dict[str, str], form: dict[str, str]) -> dict:
        async with self._http.post(f"{self._api}{path}", data=form, headers=auth) as response:
            if response.status != 200:
                # The body can echo the code; only the status goes to the log.
                raise OAuthError(f"{path} answered {response.status}")
            return await response.json()

    async def _get(self, path: str, headers: dict[str, str]) -> object:
        async with self._http.get(f"{self._api}{path}", headers=headers) as response:
            if response.status != 200:
                raise OAuthError(f"{path} answered {response.status}")
            return await response.json()

    async def _revoke(self, auth: dict[str, str], access_token: str) -> None:
        form = {"token": access_token, "token_type_hint": "access_token"}
        try:
            async with self._http.post(
                f"{self._api}/oauth2/token/revoke", data=form, headers=auth
            ) as response:
                if response.status != 200:
                    _log.warning("Discord answered %s when revoking a token", response.status)
        except aiohttp.ClientError as exc:
            _log.warning("Could not revoke a token: %s", type(exc).__name__)


def set_cookie(
    response: web.StreamResponse, name: str, value: str, *, max_age: int, secure: bool
) -> None:
    response.set_cookie(
        name, value, max_age=max_age, path="/", httponly=True, samesite="Lax", secure=secure
    )


def new_state() -> str:
    return secrets.token_urlsafe(32)


def same_state(cookie: str | None, returned: str | None) -> bool:
    return bool(cookie) and bool(returned) and secrets.compare_digest(cookie or "", returned or "")
