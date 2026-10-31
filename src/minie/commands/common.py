"""Helpers shared by the slash commands."""

import discord

from minie.errors import UserError


async def reply(interaction: discord.Interaction, message: str) -> None:
    """Answer with an ephemeral message, whether or not the interaction was deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def invoking_member(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member]:
    guild, member = interaction.guild, interaction.user
    if guild is None or not isinstance(member, discord.Member):
        raise UserError("This command only works in a server.")
    return guild, member


def voice_channel_of(member: discord.Member) -> discord.VoiceChannel:
    state = member.voice
    channel = state.channel if state is not None else None
    if channel is None:
        raise UserError("Join a voice channel first.")
    if not isinstance(channel, discord.VoiceChannel):
        raise UserError("Stage channels aren't supported, use a regular voice channel.")
    return channel
