"""Helpers shared by the slash commands and the panel buttons."""

import logging

import discord
from discord import app_commands

from minie.errors import UserError
from minie.voice.base import VoicePlayer

_log = logging.getLogger(__name__)


async def reply(interaction: discord.Interaction, message: str) -> None:
    """Answer with an ephemeral message, whether or not the interaction was deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def report_error(interaction: discord.Interaction, error: Exception, *, source: str) -> None:
    """Tell the user what went wrong: their mistake in full, anything else only as logged.

    `source` names what failed in the log, such as "/play" or "panel button".
    """
    cause = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    if isinstance(cause, UserError | app_commands.CheckFailure):
        message = str(cause)
    else:
        _log.error("%s failed", source, exc_info=cause)
        message = "Something went wrong, the error has been logged."
    try:
        await reply(interaction, message)
    except discord.HTTPException:
        # Usually the interaction expired: 3 s to answer, 15 min for follow-ups.
        _log.warning("Could not report an error to the user", exc_info=True)


def invoking_member(interaction: discord.Interaction) -> tuple[discord.Guild, discord.Member]:
    guild, member = interaction.guild, interaction.user
    if guild is None or not isinstance(member, discord.Member):
        raise UserError("This command only works in a server.")
    return guild, member


def require_listener(voice: VoicePlayer, guild_id: int, member: discord.Member) -> None:
    """Only someone in the bot's voice channel may make it play (or stop) sounds there."""
    channel_id = voice.connected_channel_id(guild_id)
    if channel_id is None:
        raise UserError("I'm not in a voice channel, use /join first.")
    state = member.voice
    if state is None or state.channel is None or state.channel.id != channel_id:
        raise UserError(f"Join <#{channel_id}> first.")


def target_channel(
    voice: VoicePlayer, guild: discord.Guild, member: discord.Member
) -> tuple[discord.VoiceChannel, bool]:
    """The member's voice channel, and whether the bot has to connect to it first.

    When it does, the bot's permissions there are checked now, before deferring, so the
    member gets the reason right away. Whether the bot may leave a channel it's busy in is
    for `PlaybackService` to decide.
    """
    channel = voice_channel_of(member)
    if voice.connected_channel_id(guild.id) == channel.id:
        return channel, False
    check_can_join(channel, guild.me)
    return channel, True


def voice_channel_of(member: discord.Member) -> discord.VoiceChannel:
    state = member.voice
    channel = state.channel if state is not None else None
    if channel is None:
        raise UserError("Join a voice channel first.")
    if not isinstance(channel, discord.VoiceChannel):
        raise UserError("Stage channels aren't supported, use a regular voice channel.")
    return channel


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
