"""What every part of the web layer shares: the services it works with, app keys, errors."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from minie.config import Settings
from minie.control import Presence, Soundboard
from minie.events import EventHub
from minie.library import SoundLibrary
from minie.playback import PlaybackService
from minie.web.auth import SESSION_COOKIE, DiscordOAuth, Session, Sessions


@dataclass(frozen=True, kw_only=True)
class Services:
    """The bot's services, and the little of its Discord state the web UI reads."""

    soundboard: Soundboard
    library: SoundLibrary
    playback: PlaybackService
    events: EventHub
    presence: Presence
    application_id: Callable[[], int | None]
    """The OAuth2 client ID; None until the bot has logged in."""
    guild_names: Callable[[], dict[int, str]]
    """The servers the bot is in."""
    ready: Callable[[], bool]
    """Whether the gateway cache is filled, so membership and voice states can be trusted."""


SERVICES = web.AppKey("services", Services)
SETTINGS = web.AppKey("settings", Settings)
SESSIONS = web.AppKey("sessions", Sessions)
OAUTH = web.AppKey("oauth", DiscordOAuth)
STREAMS: web.AppKey[set[asyncio.Task[Any]]] = web.AppKey("streams")
"""The event streams being served, cancelled on shutdown so that it needn't wait for them."""


class ApiError(Exception):
    """An answer other than success, with a message safe to show."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def session_of(request: web.Request) -> Session:
    session = request.app[SESSIONS].get(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise ApiError(401, "Sign in first.")
    return session
