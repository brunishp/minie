"""The JSON API the page calls. Every action goes through `Soundboard`, as the slash commands do.

Discord IDs are sent as strings: they exceed the 2^53 integers JavaScript numbers hold exactly.
"""

import asyncio
import re

from aiohttp import BodyPartReader, web

from minie.control import WHOLE_APP, Caller, NotAllowed, NotAMember
from minie.errors import UserError
from minie.library import NAME_RULES, SoundNotFound
from minie.playback import BusyError
from minie.store import GuildFullError, SoundExistsError
from minie.web.core import SERVICES, STREAMS, ApiError, session_of

routes = web.RouteTableDef()
HEARTBEAT = 20.0
# Multipart framing and the name field, on top of the file itself.
UPLOAD_OVERHEAD = 64 * 1024

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
        case NotAMember() | NotAllowed():
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
        # They're in it, so never "a channel you can't see", even when the bot can't view it
        # and only has a placeholder to go on.
        if caller is not None and int(channel_id) == caller.voice_channel_id:
            names[channel_id] = "your voice channel"
            continue
        name = None
        if caller is not None:
            name = await request.app[SERVICES].presence.visible_channel_name(
                caller, int(channel_id)
            )
        names[channel_id] = f"#{name}" if name else "a channel you can't see"
    readable = _CHANNEL_MENTION.sub(lambda match: names[match[1]], message)
    return readable[:1].upper() + readable[1:]


@routes.get("/api/guilds/{guild_id}/sounds")
async def sounds(request: web.Request) -> web.Response:
    caller = await _caller(request)
    services = request.app[SERVICES]
    return web.json_response(
        {
            "max_upload_bytes": services.library.max_upload_bytes,
            "name_rules": NAME_RULES,
            "sounds": [
                {
                    "name": sound.name,
                    "duration_ms": sound.duration_ms,
                    "uploader_id": str(sound.uploader_id),
                    "created_at": sound.created_at.isoformat(),
                    "can_manage": services.soundboard.can_manage(caller, sound),
                }
                for sound in services.library.sounds(caller.guild_id)
            ],
        }
    )


@routes.get("/api/guilds/{guild_id}/status")
async def status(request: web.Request) -> web.Response:
    caller = await _caller(request)
    services = request.app[SERVICES]
    current = services.playback.status(caller.guild_id)
    channel = None
    if current.channel_id is not None:
        name = await services.presence.visible_channel_name(caller, current.channel_id)
        channel = {"id": str(current.channel_id), "name": name}
    return web.json_response(
        {
            "channel": channel,
            "playing": current.playing,
            "queued": current.queued,
            "with_you": current.channel_id is not None
            and current.channel_id == caller.voice_channel_id,
            "can_leave": services.soundboard.can_leave(caller),
        }
    )


@routes.post("/api/guilds/{guild_id}/play")
async def play(request: web.Request) -> web.Response:
    caller = await _caller(request)
    await _check_command(request, caller, "play")
    name = _text(await _body(request), "name")
    soundboard = request.app[SERVICES].soundboard
    # No hurry here, unlike an interaction: the request simply waits while the bot connects.
    position = await soundboard.play(await soundboard.prepare(caller, name))
    return web.json_response({"position": position})


@routes.post("/api/guilds/{guild_id}/stop")
async def stop(request: web.Request) -> web.Response:
    caller = await _caller(request)
    await _check_command(request, caller, "stop")
    return web.json_response({"stopped": await request.app[SERVICES].soundboard.stop(caller)})


@routes.post("/api/guilds/{guild_id}/leave")
async def leave(request: web.Request) -> web.Response:
    caller = await _caller(request)
    await _check_command(request, caller, "leave")
    return web.json_response({"left": await request.app[SERVICES].soundboard.leave(caller)})


@routes.post("/api/guilds/{guild_id}/sounds")
async def add(request: web.Request) -> web.Response:
    """Multipart upload: a `name` field, then a `file` field, read under the size limit."""
    caller = await _caller(request)
    await _check_command(request, caller, "add")
    services = request.app[SERVICES]
    limit = services.library.max_upload_bytes
    if (request.content_length or 0) > limit + UPLOAD_OVERHEAD:
        raise ApiError(413, _too_big(limit))
    if request.content_type != "multipart/form-data":
        raise ApiError(400, "Send the sound as multipart/form-data.")
    reader = await request.multipart()
    name = None
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader):
            raise ApiError(400, "Nested multipart bodies aren't accepted.")
        if part.name == "name":
            name = (await _read(part, 1024, "That name is too long.")).decode(errors="replace")
        elif part.name == "file" and name is not None:
            # Name, duplicates and the server's sound count, before receiving the file.
            sound_name = services.soundboard.check_can_add(caller, name, 0)
            upload = await _read(part, limit, _too_big(limit))
            sound = await services.soundboard.add(caller, sound_name, upload)
            return web.json_response({"name": sound.name, "duration_ms": sound.duration_ms})
        else:
            raise ApiError(400, "Send a `name` field, then a `file` field.")
    raise ApiError(400, "No file was sent.")


@routes.patch("/api/guilds/{guild_id}/sounds/{name}")
async def rename(request: web.Request) -> web.Response:
    caller = await _caller(request)
    await _check_command(request, caller, "rename")
    new_name = _text(await _body(request), "name")
    _, after = request.app[SERVICES].soundboard.rename(caller, request.match_info["name"], new_name)
    return web.json_response({"name": after.name})


@routes.delete("/api/guilds/{guild_id}/sounds/{name}")
async def remove(request: web.Request) -> web.Response:
    caller = await _caller(request)
    await _check_command(request, caller, "remove")
    request.app[SERVICES].soundboard.remove(caller, request.match_info["name"])
    return web.Response(status=204)


@routes.get("/api/guilds/{guild_id}/events")
async def events(request: web.Request) -> web.StreamResponse:
    """Server-sent events naming what changed; the page fetches the new state itself."""
    caller = await _caller(request)
    response = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "X-Accel-Buffering": "no"}
    )
    await response.prepare(request)
    task = asyncio.current_task()
    assert task is not None
    request.app[STREAMS].add(task)
    try:
        with request.app[SERVICES].events.subscribe(caller.guild_id) as subscription:
            await response.write(b"retry: 3000\n\n")
            while True:
                try:
                    async with asyncio.timeout(HEARTBEAT):
                        changes = await subscription.next()
                except TimeoutError:
                    # Keeps proxies from closing an idle stream, and notices a gone client.
                    await response.write(b": heartbeat\n\n")
                    continue
                for change in changes:
                    # Browsers drop events whose data is empty, so the topic goes in both.
                    message = f"event: {change.topic}\ndata: {change.topic}\n\n"
                    await response.write(message.encode())
    except ConnectionResetError:
        pass
    finally:
        request.app[STREAMS].discard(task)
    return response


async def _caller(request: web.Request) -> Caller:
    """The signed-in member, checked against Discord now rather than at sign-in."""
    session = session_of(request)
    raw = request.match_info["guild_id"]
    if not raw.isdigit():
        raise ApiError(404, "No such server.")
    caller = await request.app[SERVICES].presence.caller(int(raw), session.user_id)
    request[CALLER] = caller
    return caller


async def _check_command(request: web.Request, caller: Caller, command: str) -> None:
    # Admins can limit a slash command to some roles or channels. Rather than reproduce how
    # Discord resolves those overwrites, refuse outright: never more than Discord allows.
    restricted = await request.app[SERVICES].presence.restricted_commands(caller.guild_id)
    if WHOLE_APP in restricted or command in restricted:
        raise NotAllowed(
            f"This server restricts /{command} to some members or channels. "
            "Use the slash command in Discord instead."
        )


async def _read(part: BodyPartReader, limit: int, too_big: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await part.read_chunk(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise ApiError(413, too_big)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_big(limit: int) -> str:
    return f"That file is over the {limit / 2**20:g} MiB limit."


async def _body(request: web.Request) -> dict[str, object]:
    try:
        body = await request.json()
    except ValueError:
        raise ApiError(400, "The request body isn't valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object.")
    return body


def _text(body: dict[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise ApiError(400, f"`{key}` must be a string.")
    return value
