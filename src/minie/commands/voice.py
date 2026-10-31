"""/join and /leave."""

import discord
from discord import app_commands

from minie.commands.common import invoking_member, reply, voice_channel_of
from minie.errors import UserError
from minie.playback import PlaybackService
from minie.voice.base import VoicePlayer


def register(tree: app_commands.CommandTree, voice: VoicePlayer, playback: PlaybackService) -> None:
    @tree.command(name="join", description="Join your voice channel")
    async def join(interaction: discord.Interaction) -> None:
        guild, member = invoking_member(interaction)
        channel = voice_channel_of(member)
        if voice.connected_channel_id(guild.id) == channel.id:
            await reply(interaction, f"I'm already in {channel.mention}.")
            return
        _check_can_join(channel, guild.me)
        # Connecting takes a voice gateway handshake, UDP IP discovery and the DAVE (MLS)
        # key exchange: often more than the 3 seconds Discord allows for a first answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await voice.join(guild.id, channel.id)
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


def _check_can_join(channel: discord.VoiceChannel, me: discord.Member) -> None:
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
