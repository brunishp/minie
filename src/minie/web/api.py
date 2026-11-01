"""The JSON API the page calls. Every action goes through `Soundboard`, as the slash commands do.

Discord IDs are sent as strings: they exceed the 2^53 integers JavaScript numbers hold exactly.
"""

import re

from aiohttp import web

from minie.control import Caller, NotAMember
from minie.errors import UserError
from minie.library import SoundNotFound
from minie.playback import BusyError
from minie.store import GuildFullError, SoundExistsError
from minie.web.core import SERVICES, session_of

routes = web.RouteTableDef()

CALLER = web.RequestKey("caller", Caller)
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")


@routes.get("/api/me")
async def me(request: web.Request) -> web.Response:
    session = session_of(request)
    names = request.app[SERVICES].guild_names()
    shared = sorted(session.guild_ids & names.keys(), key=lambda guild_id: names[guild_id].lower())
    return web.json_response(
        {
            "user": {"id": str(session.user_id), "name": session.name},
            "guilds": [{"id": str(guild_id), "name": names[guild_id]} for guild_id in shared],
        }
    )


async def user_error_response(request: web.Request, error: UserError) -> web.Response:
    status = 400
    match error:
        case NotAMember():
            status = 403
        case SoundNotFound():
            status = 404
        case BusyError() | SoundExistsError() | GuildFullError():
            status = 409
    return web.json_response({"error": await _readable(request, str(error))}, status=status)


async def _readable(request: web.Request, message: str) -> str:
    """Replace Discord channel mentions, which a web page can't render, with names the member
    may see."""
    caller = request.get(CALLER)
    names: dict[str, str] = {}
    for channel_id in set(_CHANNEL_MENTION.findall(message)):
        name = None
        if caller is not None:
            name = await request.app[SERVICES].presence.visible_channel_name(
                caller, int(channel_id)
            )
        names[channel_id] = f"#{name}" if name else "a channel you can't see"
    return _CHANNEL_MENTION.sub(lambda match: names[match[1]], message)
