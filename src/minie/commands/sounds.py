"""/play. For now it plays the built-in test sound."""

import asyncio
from pathlib import Path

import discord
from discord import app_commands

from minie.commands.common import invoking_member, reply
from minie.errors import UserError
from minie.voice.base import VoicePlayer
from minie.voice.ogg import read_opus_frames


def register(tree: app_commands.CommandTree, voice: VoicePlayer, *, test_tone: Path) -> None:
    @tree.command(name="play", description="Play the test sound in my voice channel")
    async def play(interaction: discord.Interaction) -> None:
        guild, member = invoking_member(interaction)
        channel_id = voice.connected_channel_id(guild.id)
        if channel_id is None:
            raise UserError("I'm not in a voice channel, use /join first.")
        state = member.voice
        if state is None or state.channel is None or state.channel.id != channel_id:
            raise UserError(f"Join <#{channel_id}> to play sounds there.")

        frames = await asyncio.to_thread(read_opus_frames, test_tone)
        # A new sound cuts off the current one.
        await voice.stop(guild.id)
        await voice.play(guild.id, frames)
        await reply(interaction, "Playing the test sound.")
