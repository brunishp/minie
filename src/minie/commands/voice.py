"""/join and /leave."""

import discord
from discord import app_commands

from minie.commands.common import invoking_caller, reply
from minie.control import Soundboard


def register(tree: app_commands.CommandTree, soundboard: Soundboard) -> None:
    @tree.command(name="join", description="Join your voice channel")
    async def join(interaction: discord.Interaction) -> None:
        caller = invoking_caller(interaction)
        channel_id, must_connect = soundboard.target_channel(caller)
        if not must_connect:
            await reply(interaction, f"I'm already in <#{channel_id}>.")
            return
        # Connecting takes a voice gateway handshake, UDP IP discovery and the DAVE (MLS)
        # key exchange: often more than the 3 seconds Discord allows for a first answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await soundboard.join(caller)
        await reply(interaction, f"Joined <#{channel_id}>.")

    @tree.command(name="leave", description="Leave the voice channel")
    async def leave(interaction: discord.Interaction) -> None:
        caller = invoking_caller(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        was_connected = await soundboard.leave(caller)  # also drops queued sounds
        await reply(interaction, "Left the voice channel." if was_connected else "I wasn't in one.")
