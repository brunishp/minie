"""/play, /stop, /say, /add, /list, /info, /rename and /remove."""

import discord
from discord import app_commands

from minie.commands.common import invoking_caller, invoking_member, reply
from minie.control import Soundboard
from minie.library import NAME_RULES, SoundLibrary
from minie.store import Sound

MESSAGE_LIMIT = 2000


def register(
    tree: app_commands.CommandTree, *, soundboard: Soundboard, library: SoundLibrary
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
        prepared = await soundboard.prepare(invoking_caller(interaction), sound)
        label = "the test tone" if prepared.sound is None else f"`{prepared.sound.name}`"
        if prepared.must_connect:
            # Joining first can take longer than the 3 s Discord allows for an answer.
            await interaction.response.defer(ephemeral=True, thinking=True)
        position = await soundboard.play(prepared)
        await reply(
            interaction,
            f"Playing {label}." if position == 0 else f"Queued {label} (position {position}).",
        )

    @tree.command(name="stop", description="Stop the current sound and clear the queue")
    async def stop(interaction: discord.Interaction) -> None:
        stopped = await soundboard.stop(invoking_caller(interaction))
        await reply(interaction, "Stopped." if stopped else "Nothing was playing.")

    # Only with a voice configured, so that it doesn't show up where it can't work.
    if soundboard.can_speak:

        @tree.command(name="say", description="Say something in your voice channel")
        @app_commands.describe(text="What to say, up to 200 characters")
        async def say(
            interaction: discord.Interaction, text: app_commands.Range[str, 1, 200]
        ) -> None:
            caller = invoking_caller(interaction)
            soundboard.check_can_say(caller, text)
            # Generating speech and maybe joining can take longer than Discord's 3 s.
            await interaction.response.defer(ephemeral=True, thinking=True)
            position = await soundboard.play(await soundboard.prepare_speech(caller, text))
            await reply(
                interaction,
                "Saying it." if position == 0 else f"Queued it (position {position}).",
            )

    @tree.command(name="add", description="Add a sound from an audio file")
    @app_commands.describe(name=NAME_RULES, file="MP3, WAV, Ogg, FLAC, M4A or WebM")
    async def add(
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        file: discord.Attachment,
    ) -> None:
        caller = invoking_caller(interaction)
        sound_name = soundboard.check_can_add(caller, name, file.size)
        # Downloading and two FFmpeg passes take far longer than the 3 s Discord allows.
        await interaction.response.defer(ephemeral=True, thinking=True)
        sound = await soundboard.add(caller, sound_name, await file.read())
        await reply(interaction, f"Added `{sound.name}` ({_seconds(sound)}).")

    @tree.command(name="list", description="List this server's sounds")
    async def list_sounds(interaction: discord.Interaction) -> None:
        guild, _ = invoking_member(interaction)
        await reply(interaction, _format_list(library.sounds(guild.id)))

    @tree.command(name="info", description="Show who added a sound, when, and its length")
    @app_commands.describe(sound="Name of the sound")
    @app_commands.autocomplete(sound=sound_choices)
    async def info(interaction: discord.Interaction, sound: str) -> None:
        guild, _ = invoking_member(interaction)
        await reply(interaction, _format_info(library.find(guild.id, sound)))

    @tree.command(name="rename", description="Rename a sound")
    @app_commands.describe(sound="Name of the sound to rename", new_name=NAME_RULES)
    @app_commands.autocomplete(sound=sound_choices)
    async def rename(
        interaction: discord.Interaction,
        sound: str,
        new_name: app_commands.Range[str, 1, 32],
    ) -> None:
        before, after = soundboard.rename(invoking_caller(interaction), sound, new_name)
        await reply(interaction, f"Renamed `{before.name}` to `{after.name}`.")

    @tree.command(name="remove", description="Remove a sound")
    @app_commands.describe(sound="Name of the sound to remove")
    @app_commands.autocomplete(sound=sound_choices)
    async def remove(interaction: discord.Interaction, sound: str) -> None:
        removed = soundboard.remove(invoking_caller(interaction), sound)
        await reply(interaction, f"Removed `{removed.name}`.")


def _seconds(sound: Sound) -> str:
    return f"{sound.duration_ms / 1000:.1f} s"


def _format_info(sound: Sound) -> str:
    # The mention never pings (the client allows no mentions), and Discord shows the
    # timestamps in each reader's own time zone.
    added = discord.utils.format_dt(sound.created_at, "D")
    ago = discord.utils.format_dt(sound.created_at, "R")
    return f"`{sound.name}` · {_seconds(sound)}\nAdded by <@{sound.uploader_id}> on {added} ({ago})"


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
