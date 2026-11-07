"""The Discord client: gateway session, slash command tree and the services behind it."""

import asyncio
import contextlib
import logging
from collections.abc import Sequence

import discord
from discord import app_commands

from minie.commands import panel, sounds
from minie.commands import voice as voice_commands
from minie.commands.common import report_error
from minie.config import Settings
from minie.control import DiscordPresence, Soundboard
from minie.events import EventHub, Topic
from minie.library import SoundLibrary
from minie.playback import PlaybackService
from minie.speech import PiperWorker, Speech
from minie.store import SoundStore
from minie.voice.base import VoicePlayer
from minie.voice.discordpy import DiscordPyVoicePlayer
from minie.web import Services

_log = logging.getLogger(__name__)

TEST_TONE_FILE = "builtin/test-tone.ogg"


class MinieBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        # Least privilege: the guild and channel cache plus voice states are all the bot
        # reads. Neither intent is privileged, and slash commands need no message content.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        # Replies may echo what users typed; the bot never pings anyone.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())

        self.settings = settings
        self.test_tone = settings.data_dir / TEST_TONE_FILE
        self.events = EventHub()
        self.voice: VoicePlayer = DiscordPyVoicePlayer(self)
        self.store = SoundStore(settings.data_dir / "minie.db")
        self.library = SoundLibrary(self.store, settings, self.events)
        self.playback = PlaybackService(
            self.voice,
            overlap=settings.overlap,
            queue_max=settings.queue_max,
            cooldown=settings.cooldown,
            events=self.events,
        )
        self.presence = DiscordPresence(self)
        self.speech = None
        if settings.tts_voice is not None:
            self.speech = Speech(
                PiperWorker(
                    settings.piper,
                    settings.tts_voice,
                    settings.data_dir / "tmp" / "speech",
                    keep=settings.tts_keep,
                ),
                ffmpeg=settings.ffmpeg,
                pitch=settings.tts_pitch,
                loudness_lufs=settings.loudness_lufs,
                true_peak_dbtp=settings.true_peak_dbtp,
            )
        self.soundboard = Soundboard(
            voice=self.voice,
            playback=self.playback,
            library=self.library,
            presence=self.presence,
            test_tone=self.test_tone,
            speaker=self.speech,
        )
        self.tree = app_commands.CommandTree(
            self,
            allowed_contexts=app_commands.AppCommandContext(guild=True),
            allowed_installs=app_commands.AppInstallationType(guild=True),
        )
        self.tree.error(_on_command_error)
        # Read by the panel's buttons through `interaction.client.panels`.
        self.panels = panel.Panels(soundboard=self.soundboard, library=self.library)
        self._background: set[asyncio.Task[None]] = set()
        voice_commands.register(self.tree, self.soundboard)
        sounds.register(self.tree, soundboard=self.soundboard, library=self.library)
        panel.register(self.tree, self.panels)

    def web_services(self) -> Services:
        return Services(
            soundboard=self.soundboard,
            library=self.library,
            playback=self.playback,
            events=self.events,
            presence=self.presence,
            application_id=lambda: self.application_id,
            guild_names=lambda: {guild.id: guild.name for guild in self.guilds},
            ready=self.is_ready,
        )

    async def setup_hook(self) -> None:
        # Lets panels opened before a restart keep working.
        self.add_dynamic_items(*panel.BUTTONS)
        self._background.add(asyncio.create_task(self.panels.follow(self.events)))

    async def on_ready(self) -> None:
        _log.info("Connected as %s, in %d guild(s)", self.user, len(self.guilds))

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        # The bot itself moved or disconnected by a moderator, or by a network drop.
        if self.user is not None and member.id == self.user.id:
            self.events.publish(member.guild.id, Topic.STATUS)

    async def on_raw_app_command_permissions_update(
        self, payload: discord.RawAppCommandPermissionsUpdateEvent
    ) -> None:
        # Someone changed Server Settings > Integrations: the web UI checks afresh.
        self.presence.forget_restrictions(payload.guild.id)

    async def close(self) -> None:
        for task in self._background:
            task.cancel()
        for task in self._background:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.speech is not None:
            await self.speech.close()
        await super().close()
        self.store.close()


async def sync_commands(
    tree: app_commands.CommandTree, *, guild_id: int | None, clear: bool = False
) -> None:
    if guild_id is None:
        synced = await tree.sync()
        _log.info(
            "Registered %d global command(s): %s. Clients can take a while to show them.",
            len(synced),
            _names(synced),
        )
        return

    guild = discord.Object(id=guild_id)
    if clear:
        tree.clear_commands(guild=guild)
    else:
        # Guild commands update instantly, unlike global ones: the setup for development.
        tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)
    _log.info("Registered %d command(s) in guild %s: %s", len(synced), guild_id, _names(synced))


def _names(commands: Sequence[app_commands.AppCommand]) -> str:
    return ", ".join(f"/{command.name}" for command in commands) or "none"


async def _on_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    command = interaction.command.qualified_name if interaction.command else "?"
    await report_error(interaction, error, source=f"/{command}")
