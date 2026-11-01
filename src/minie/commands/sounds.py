"""/play, /stop, /add, /list and /remove."""

import asyncio
from pathlib import Path

import discord
from discord import app_commands

from minie.commands.common import invoking_member, reply, require_listener, target_channel
from minie.commands.panel import Panels
from minie.errors import UserError
from minie.library import NAME_RULES, SoundLibrary
from minie.playback import PlaybackService
from minie.store import Sound
from minie.voice.base import VoicePlayer
from minie.voice.ogg import read_opus_frames

MESSAGE_LIMIT = 2000


def register(
    tree: app_commands.CommandTree,
    *,
    voice: VoicePlayer,
    playback: PlaybackService,
    library: SoundLibrary,
    panels: Panels,
    test_tone: Path,
) -> None:
    async def sound_choices(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        return [
            app_commands.Choice(name=f"{sound.name} ({_seconds(sound)})", value=sound.name)
            for sound in library.search(interaction.guild_id, current)
        ]

    @tree.command(name="play", description="Play a sound, or the test tone if none is given")
    @app_commands.describe(sound="Name of the sound (see /list)")
    @app_commands.autocomplete(sound=sound_choices)
    async def play(interaction: discord.Interaction, sound: str | None = None) -> None:
        guild, member = invoking_member(interaction)
        channel, must_connect = target_channel(voice, guild, member)
        if sound is None:
            label = "the test tone"
            frames = await asyncio.to_thread(read_opus_frames, test_tone)
        else:
            found = library.find(guild.id, sound)
            label = f"`{found.name}`"
            frames = await library.frames(found)
        if must_connect:
            # Joining first can take longer than the 3 s Discord allows for an answer.
            await interaction.response.defer(ephemeral=True, thinking=True)
        position = await playback.play(guild.id, member.id, channel.id, frames)
        await reply(
            interaction,
            f"Playing {label}." if position == 0 else f"Queued {label} (position {position}).",
        )

    @tree.command(name="stop", description="Stop the current sound and clear the queue")
    async def stop(interaction: discord.Interaction) -> None:
        guild, member = invoking_member(interaction)
        require_listener(voice, guild.id, member)
        stopped = await playback.stop(guild.id)
        await reply(interaction, "Stopped." if stopped else "Nothing was playing.")

    @tree.command(name="add", description="Add a sound from an audio file")
    @app_commands.describe(name=NAME_RULES, file="MP3, WAV, Ogg, FLAC, M4A or WebM")
    async def add(
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        file: discord.Attachment,
    ) -> None:
        guild, member = invoking_member(interaction)
        sound_name = library.check_can_add(guild.id, name, file.size)
        # Downloading and two FFmpeg passes take far longer than the 3 s Discord allows.
        await interaction.response.defer(ephemeral=True, thinking=True)
        sound = await library.add(guild.id, member.id, sound_name, await file.read())
        await reply(interaction, f"Added `{sound.name}` ({_seconds(sound)}).")
        await panels.refresh(guild.id)  # after replying: the uploader needn't wait for it

    @tree.command(name="list", description="List this server's sounds")
    async def list_sounds(interaction: discord.Interaction) -> None:
        guild, _ = invoking_member(interaction)
        await reply(interaction, _format_list(library.sounds(guild.id)))

    @tree.command(name="remove", description="Remove a sound")
    @app_commands.describe(sound="Name of the sound to remove")
    @app_commands.autocomplete(sound=sound_choices)
    async def remove(interaction: discord.Interaction, sound: str) -> None:
        guild, member = invoking_member(interaction)
        found = library.find(guild.id, sound)
        if found.uploader_id != member.id and not member.guild_permissions.manage_guild:
            raise UserError(
                "Only whoever added a sound, or someone with Manage Server, can remove it."
            )
        library.remove(found)
        await reply(interaction, f"Removed `{found.name}`.")
        await panels.refresh(guild.id)


def _seconds(sound: Sound) -> str:
    return f"{sound.duration_ms / 1000:.1f} s"


def _format_list(sounds: list[Sound]) -> str:
    if not sounds:
        return "No sounds yet. Add one with /add."
    lines = [f"{len(sounds)} sound{'s' if len(sounds) > 1 else ''}:"]
    for index, sound in enumerate(sounds):
        line = f"`{sound.name}` {_seconds(sound)}"
        more = f"… and {len(sounds) - index} more"
        if sum(len(entry) + 1 for entry in lines) + len(line) + len(more) + 2 > MESSAGE_LIMIT:
            lines.append(more)
            break
        lines.append(line)
    return "\n".join(lines)
