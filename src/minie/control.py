"""What members may do with the bot, whoever asks: a slash command, a panel button or the web UI.

The rules see a member as a `Caller`, and ask `Presence` whatever they need to know about
Discord itself (the bot's permissions in a channel, what a member can see). Tests use fakes of
both, as `FakeVoice` stands in for the voice layer.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import discord

from minie.errors import UserError
from minie.library import SoundLibrary
from minie.playback import PlaybackService
from minie.store import Sound
from minie.voice.base import VoicePlayer
from minie.voice.ogg import read_opus_frames

# How long a member fetched over REST counts as current. Roles (hence permissions) can be up to
# this old; members in voice are always current, from the gateway cache.
MEMBER_TTL = 60.0
# How long a server's slash command permissions count as current, unless Discord announces a
# change first.
COMMAND_PERMISSIONS_TTL = 60.0
WHOLE_APP = "*"
"""Stands for an overwrite on the whole application rather than one command."""


class NotAMember(UserError):
    pass


class NotAllowed(UserError):
    pass


@dataclass(frozen=True, kw_only=True)
class Caller:
    """The member behind an action, as far as the rules are concerned."""

    guild_id: int
    user_id: int
    voice_channel_id: int | None
    """Their voice channel; None when in none, or in a stage channel."""
    in_stage: bool = False
    manage_guild: bool = False

    @classmethod
    def of(cls, member: discord.Member) -> Caller:
        channel = member.voice.channel if member.voice is not None else None
        in_stage = isinstance(channel, discord.StageChannel)
        return cls(
            guild_id=member.guild.id,
            user_id=member.id,
            voice_channel_id=channel.id if channel is not None and not in_stage else None,
            in_stage=in_stage,
            manage_guild=member.guild_permissions.manage_guild,
        )


class Presence(Protocol):
    """What the rules need to know about Discord, beyond the caller."""

    def check_can_join(self, guild_id: int, channel_id: int) -> None:
        """Raise `UserError` naming what keeps the bot out of the channel."""
        ...

    async def caller(self, guild_id: int, user_id: int) -> Caller:
        """The member `user_id` of `guild_id`. Raise `NotAMember` if they aren't one."""
        ...

    async def visible_channel_name(self, caller: Caller, channel_id: int) -> str | None:
        """The channel's name if `caller` may view the channel, else None."""
        ...

    async def restricted_commands(self, guild_id: int) -> frozenset[str]:
        """The slash commands the server restricts in Server Settings > Integrations, and
        `WHOLE_APP` when it restricts the whole application."""
        ...


class DiscordPresence:
    """`Presence` answered from discord.py's caches, with REST for members not in voice."""

    def __init__(self, client: discord.Client, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._fetched: dict[tuple[int, int], tuple[float, discord.Member | None]] = {}
        self._restrictions: dict[int, tuple[float, frozenset[str]]] = {}

    def check_can_join(self, guild_id: int, channel_id: int) -> None:
        guild = self._client.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if guild is None or not isinstance(channel, discord.VoiceChannel):
            raise UserError("I can't find that voice channel.")
        check_can_join(channel, guild.me)

    async def caller(self, guild_id: int, user_id: int) -> Caller:
        member = await self._member(guild_id, user_id)
        if member is None:
            raise NotAMember("You're not a member of that server, or I'm not in it.")
        return Caller.of(member)

    async def visible_channel_name(self, caller: Caller, channel_id: int) -> str | None:
        guild = self._client.get_guild(caller.guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        member = await self._member(caller.guild_id, caller.user_id)
        if channel is None or member is None:
            return None
        # From 2026-11-16 the bot may know channels the member can't see: never name those.
        return channel.name if channel.permissions_for(member).view_channel else None

    async def restricted_commands(self, guild_id: int) -> frozenset[str]:
        now = self._clock()
        cached = self._restrictions.get(guild_id)
        if cached is None or now - cached[0] >= COMMAND_PERMISSIONS_TTL:
            try:
                cached = (now, await self._fetch_restrictions(guild_id))
            except discord.HTTPException:
                # Unknown means restricted: the web UI must never allow more than Discord.
                raise UserError(
                    "I couldn't check this server's command permissions, try again later."
                ) from None
            self._restrictions[guild_id] = cached
        return cached[1]

    def forget_restrictions(self, guild_id: int) -> None:
        """Discord announced a change to the server's command permissions."""
        self._restrictions.pop(guild_id, None)

    async def _fetch_restrictions(self, guild_id: int) -> frozenset[str]:
        application_id = self._client.application_id
        if application_id is None:
            raise UserError("I'm still starting up, try again in a moment.")
        http = self._client.http
        # Commands are global, or copied to the development server: name them from both.
        commands = [
            *await http.get_global_commands(application_id),
            *await http.get_guild_commands(application_id, guild_id),
        ]
        names = {int(command["id"]): command["name"] for command in commands}
        restricted: set[str] = set()
        for entry in await http.get_guild_application_command_permissions(application_id, guild_id):
            if not entry["permissions"]:
                continue
            target = int(entry["id"])
            if target == application_id:
                restricted.add(WHOLE_APP)
            elif target in names:
                restricted.add(names[target])
        return frozenset(restricted)

    async def _member(self, guild_id: int, user_id: int) -> discord.Member | None:
        guild = self._client.get_guild(guild_id)
        if guild is None:
            return None
        # Cached: members in voice (MemberCacheFlags.voice, with the voice_states intent).
        if (member := guild.get_member(user_id)) is not None:
            return member
        now = self._clock()
        key = (guild_id, user_id)
        if (cached := self._fetched.get(key)) is not None and now - cached[0] < MEMBER_TTL:
            return cached[1]
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            member = None
        self._fetched = {k: v for k, v in self._fetched.items() if now - v[0] < MEMBER_TTL}
        self._fetched[key] = (now, member)
        return member


def check_can_join(channel: discord.VoiceChannel, me: discord.Member) -> None:
    # Replies name channels by mention only, which each user's client renders from its own
    # view. From 2026-11-16, Discord sends the bot channels it can't view with a placeholder
    # name; those fail the View Channel check below.
    permissions = channel.permissions_for(me)
    required = (
        ("View Channel", permissions.view_channel),
        ("Connect", permissions.connect),
        ("Speak", permissions.speak),
    )
    missing = [name for name, granted in required if not granted]
    if missing:
        raise UserError(f"I'm missing permissions in {channel.mention}: {', '.join(missing)}.")
    full = channel.user_limit and len(channel.members) >= channel.user_limit
    if full and not permissions.move_members:
        raise UserError(f"{channel.mention} is full.")


@dataclass(frozen=True)
class PreparedSound:
    """A sound checked and loaded, ready for `Soundboard.play`."""

    caller: Caller
    channel_id: int
    must_connect: bool
    """Playing it connects the bot first, which can take longer than Discord's 3 s deadline."""
    sound: Sound | None
    """None for the test tone."""
    frames: list[bytes]


class Soundboard:
    def __init__(
        self,
        *,
        voice: VoicePlayer,
        playback: PlaybackService,
        library: SoundLibrary,
        presence: Presence,
        test_tone: Path,
    ) -> None:
        self._voice = voice
        self._playback = playback
        self._library = library
        self._presence = presence
        self._test_tone = test_tone

    def target_channel(self, caller: Caller) -> tuple[int, bool]:
        """The caller's voice channel, and whether the bot has to connect to it first.

        When it does, the bot's permissions there are checked now, so the member gets the
        reason before the slow part. Whether the bot may leave a channel it's busy in is for
        `PlaybackService` to decide.
        """
        if caller.in_stage:
            raise UserError("Stage channels aren't supported, use a regular voice channel.")
        if caller.voice_channel_id is None:
            raise UserError("Join a voice channel first.")
        if self._voice.connected_channel_id(caller.guild_id) == caller.voice_channel_id:
            return caller.voice_channel_id, False
        self._presence.check_can_join(caller.guild_id, caller.voice_channel_id)
        return caller.voice_channel_id, True

    def require_listener(self, caller: Caller) -> None:
        """Only someone in the bot's voice channel may stop what plays there."""
        channel_id = self._voice.connected_channel_id(caller.guild_id)
        if channel_id is None:
            raise UserError("I'm not in a voice channel.")
        if caller.voice_channel_id != channel_id:
            raise UserError(f"Join <#{channel_id}> first.")

    async def join(self, caller: Caller) -> int:
        channel_id, must_connect = self.target_channel(caller)
        if must_connect:
            await self._playback.join(caller.guild_id, channel_id)
        return channel_id

    async def prepare(self, caller: Caller, name: str | None) -> PreparedSound:
        """Check and load a sound, the test tone when `name` is None."""
        channel_id, must_connect = self.target_channel(caller)
        if name is None:
            sound = None
            frames = await asyncio.to_thread(read_opus_frames, self._test_tone)
        else:
            sound = self._library.find(caller.guild_id, name)
            frames = await self._library.frames(sound)
        return PreparedSound(caller, channel_id, must_connect, sound, frames)

    async def play(self, prepared: PreparedSound) -> int:
        """Returns the queue position, 0 meaning now."""
        caller = prepared.caller
        return await self._playback.play(
            caller.guild_id, caller.user_id, prepared.channel_id, prepared.frames
        )

    async def stop(self, caller: Caller) -> bool:
        self.require_listener(caller)
        return await self._playback.stop(caller.guild_id)

    async def leave(self, caller: Caller) -> bool:
        return await self._playback.leave(caller.guild_id)

    def can_manage(self, caller: Caller, sound: Sound) -> bool:
        return sound.uploader_id == caller.user_id or caller.manage_guild

    def check_can_add(self, caller: Caller, name: str, size: int) -> str:
        """Cheap checks before receiving the file. Returns the normalised name."""
        return self._library.check_can_add(caller.guild_id, name, size)

    async def add(self, caller: Caller, name: str, upload: bytes) -> Sound:
        return await self._library.add(caller.guild_id, caller.user_id, name, upload)

    def rename(self, caller: Caller, name: str, new_name: str) -> tuple[Sound, Sound]:
        """Returns the sound before and after."""
        sound = self._managed(caller, name, "rename")
        return sound, self._library.rename(sound, new_name)

    def remove(self, caller: Caller, name: str) -> Sound:
        sound = self._managed(caller, name, "remove")
        self._library.remove(sound)
        return sound

    def _managed(self, caller: Caller, name: str, action: str) -> Sound:
        sound = self._library.find(caller.guild_id, name)
        if not self.can_manage(caller, sound):
            raise NotAllowed(
                f"Only whoever added a sound, or someone with Manage Server, can {action} it."
            )
        return sound
