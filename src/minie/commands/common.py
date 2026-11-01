"""Helpers shared by the slash commands and the panel buttons."""

import logging

import discord
from discord import app_commands

from minie.control import Caller
from minie.errors import UserError

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


def invoking_caller(interaction: discord.Interaction) -> Caller:
    _, member = invoking_member(interaction)
    return Caller.of(member)
