"""A running web UI wired to fakes, plus a fake Discord OAuth2 server, for the web tests."""

import base64
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer
from fakes import STEREO_20MS, FakePresence, FakeVoice, write_opus

from minie.config import Settings
from minie.control import Caller, Soundboard
from minie.events import EventHub
from minie.library import SoundLibrary
from minie.playback import Overlap, PlaybackService
from minie.store import Sound, SoundStore
from minie.web import Services, create_app
from minie.web.app import runner_options

GUILD = 1
OTHER_GUILD = 99
APPLICATION_ID = 4242
SECRET = "client-secret"
ALICE, BOB = 10, 11
HERE, ELSEWHERE = 2, 3
HORN = [STEREO_20MS + b"horn"]


@dataclass
class FakeDiscord:
    """Discord's OAuth2 endpoints, as much as the sign-in uses."""

    users: dict[str, dict[str, str]] = field(default_factory=dict)
    """Authorisation code → the user it signs in."""
    guilds: list[dict[str, str]] = field(default_factory=lambda: [{"id": str(GUILD)}])
    revoked: list[str] = field(default_factory=list)
    redirect_uri: str = ""

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/oauth2/token", self._token)
        app.router.add_post("/api/oauth2/token/revoke", self._revoke)
        app.router.add_get("/api/users/@me", self._me)
        app.router.add_get("/api/users/@me/guilds", self._guilds)
        return app

    def _check_client(self, request: web.Request) -> None:
        encoded = request.headers["Authorization"].removeprefix("Basic ")
        assert base64.b64decode(encoded).decode() == f"{APPLICATION_ID}:{SECRET}"

    async def _token(self, request: web.Request) -> web.Response:
        self._check_client(request)
        form = await request.post()
        assert form["grant_type"] == "authorization_code"
        assert form["redirect_uri"] == self.redirect_uri
        if form["code"] not in self.users:
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response({"access_token": f"access-{form['code']}"})

    async def _revoke(self, request: web.Request) -> web.Response:
        self._check_client(request)
        self.revoked.append(str((await request.post())["token"]))
        return web.json_response({})

    def _user(self, request: web.Request) -> dict[str, str]:
        token = request.headers["Authorization"].removeprefix("Bearer access-")
        return self.users[token]

    async def _me(self, request: web.Request) -> web.Response:
        return web.json_response(self._user(request))

    async def _guilds(self, request: web.Request) -> web.Response:
        self._user(request)
        return web.json_response(self.guilds)


class Harness:
    def __init__(self, tmp_path: Path, store: SoundStore) -> None:
        self.tmp_path = tmp_path
        self.store = store
        self.events = EventHub()
        self.voice = FakeVoice()
        self.presence = FakePresence()
        self.library = SoundLibrary(store, Settings(data_dir=tmp_path), self.events)
        self.playback = PlaybackService(
            self.voice, overlap=Overlap.INTERRUPT, queue_max=5, cooldown=0, events=self.events
        )
        self.soundboard = Soundboard(
            voice=self.voice,
            playback=self.playback,
            library=self.library,
            presence=self.presence,
            test_tone=write_opus(tmp_path / "tone.ogg", HORN),
        )
        self.ready = True
        self.discord = FakeDiscord()
        self.client: TestClient
        self.url = ""

    def services(self) -> Services:
        return Services(
            soundboard=self.soundboard,
            library=self.library,
            playback=self.playback,
            events=self.events,
            presence=self.presence,
            application_id=lambda: APPLICATION_ID,
            guild_names=lambda: {GUILD: "Test server"},
            ready=lambda: self.ready,
        )

    def member(self, user_id: int, channel: int | None = HERE, **extra: bool) -> Caller:
        return self.presence.add(
            Caller(guild_id=GUILD, user_id=user_id, voice_channel_id=channel, **extra)
        )

    def sound(self, name: str, uploader: int = ALICE) -> Sound:
        sound = Sound(f"{name}-id", GUILD, name, uploader, 20, datetime(2026, 11, 1, tzinfo=UTC))
        self.store.add(sound, max_per_guild=100)
        write_opus(self.tmp_path / "sounds" / str(GUILD) / f"{sound.id}.ogg", HORN)
        return sound

    async def sign_in(self, user_id: int = ALICE, name: str = "Alice") -> None:
        code = f"code-{user_id}"
        self.discord.users[code] = {
            "id": str(user_id),
            "username": name.lower(),
            "global_name": name,
        }
        login = await self.client.get("/auth/login", allow_redirects=False)
        assert login.status == 302
        state = parse_qs(urlsplit(login.headers["Location"]).query)["state"][0]
        callback = await self.client.get(
            "/auth/callback", params={"code": code, "state": state}, allow_redirects=False
        )
        assert callback.status == 302, await callback.text()

    async def post(self, path: str, json: object = None, **kwargs: Any) -> ClientResponse:
        return await self.client.post(
            path, json=json if json is not None else {}, headers={"Origin": self.url}, **kwargs
        )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@asynccontextmanager
async def running(harness: Harness) -> AsyncIterator[Harness]:
    async with TestServer(harness.discord.app()) as discord_server:
        port = free_port()
        harness.url = f"http://127.0.0.1:{port}"
        harness.discord.redirect_uri = f"{harness.url}/auth/callback"
        settings = Settings(
            data_dir=harness.tmp_path,
            web_port=port,
            web_public_url=harness.url,
            oauth_client_secret=SECRET,
        )
        app = create_app(
            harness.services(),
            settings,
            discord_api=str(discord_server.make_url("/api")),
            discord_authorize=str(discord_server.make_url("/authorize")),
        )
        server = TestServer(app, host="127.0.0.1", port=port)
        # Runner options only reach aiohttp through start_server, not the constructor.
        await server.start_server(**runner_options())
        async with TestClient(server) as client:
            harness.client = client
            yield harness
