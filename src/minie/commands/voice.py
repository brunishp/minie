"""/join and /leave."""

import discord
from discord import app_commands

from minie.commands.common import invoking_member, reply, target_channel
from minie.playback import PlaybackService
from minie.voice.base import VoicePlayer


def register(tree: app_commands.CommandTree, voice: VoicePlayer, playback: PlaybackService) -> None:
    @tree.command(name="join", description="Join your voice channel")
    async def join(interaction: discord.Interaction) -> None:
        guild, member = invoking_member(interaction)
        channel, must_connect = target_channel(voice, guild, member)
        if not must_connect:
            await reply(interaction, f"I'm already in {channel.mention}.")
            return
        # Connecting takes a voice gateway handshake, UDP IP discovery and the DAVE (MLS)
        # key exchange: often more than the 3 seconds Discord allows for a first answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await playback.join(guild.id, channel.id)
        await reply(interaction, f"Joined {channel.mention}.")

    @tree.command(name="leave", description="Leave the voice channel")
    async def leave(interaction: discord.Interaction) -> None:
        guild, _ = invoking_member(interaction)
        was_connected = voice.connected_channel_id(guild.id) is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await playback.stop(guild.id)  # also drops queued sounds
        # Called even when not connected, to clear a connection left half-open.
        await voice.leave(guild.id)
        await reply(interaction, "Left the voice channel." if was_connected else "I wasn't in one.")
