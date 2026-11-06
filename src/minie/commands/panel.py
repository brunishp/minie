"""/panel: a private grid of sound buttons, one per member who opens it.

The panel is an ephemeral message, so each member pages on their own and nothing is posted
in channels. Its buttons are `DynamicItem`s: their state (page, sound name) lives in their
`custom_id`, so they keep working after a restart without anything being stored.
"""

import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Protocol, Self, cast

import discord
from discord import app_commands

from minie.commands.common import invoking_caller, invoking_member, report_error
from minie.control import Soundboard
from minie.errors import UserError
from minie.events import EventHub, Topic
from minie.library import SoundLibrary, SoundNotFound
from minie.store import Sound

_log = logging.getLogger(__name__)

# A message holds at most 25 buttons in 5 rows: four rows of sounds, one of controls.
PAGE_SIZE = 20
ROW_SIZE = 5
# Ephemeral messages can only be edited with an interaction token, which Discord accepts
# for 15 minutes. Stop a little earlier rather than race the expiry.
EDIT_WINDOW = timedelta(minutes=14)


class Drawing(NamedTuple):
    content: str
    view: discord.ui.View | None
    page: int
    """The page actually drawn, after clamping."""


def render(sounds: Sequence[Sound], page: int) -> Drawing:
    """Draw one page of the panel. Pages past the end show the last one."""
    if not sounds:
        return Drawing("No sounds yet. Add one with /add.", None, 0)
    pages = math.ceil(len(sounds) / PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    view = discord.ui.View(timeout=None)
    for index, sound in enumerate(sounds[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]):
        view.add_item(PlayButton(page, sound.name, row=index // ROW_SIZE))
    view.add_item(PageButton(page - 1, "previous", disabled=page == 0))
    view.add_item(PageButton(page + 1, "next", disabled=page == pages - 1))
    view.add_item(StopButton(page))
    count = f"{len(sounds)} sound{'s' if len(sounds) > 1 else ''}"
    content = (
        f"**Soundboard** · page {page + 1}/{pages} · {count}\n"
        "Click a sound from any voice channel, and I'll join you."
    )
    return Drawing(content, view, page)


@dataclass
class _OpenPanel:
    interaction: discord.Interaction
    page: int


class PanelRegistry:
    """The panels that can still be edited, so they can follow /add and /remove.

    Keeps each member's latest interaction with their panel, in memory only: its token can
    edit that message, so it's a credential. Entries are dropped once the token is too old,
    or when an edit fails (typically a panel the member dismissed). After a restart the
    registry is empty, and panels catch up on their next click.
    """

    def __init__(self, clock: Callable[[], datetime] = discord.utils.utcnow) -> None:
        self._clock = clock
        self._open: dict[tuple[int, int], _OpenPanel] = {}

    def track(self, interaction: discord.Interaction, page: int) -> None:
        if interaction.guild_id is not None:
            key = (interaction.guild_id, interaction.user.id)
            self._open[key] = _OpenPanel(interaction, page)

    async def refresh(self, guild_id: int, sounds: Sequence[Sound]) -> None:
        now = self._clock()
        for key, panel in list(self._open.items()):
            if key[0] != guild_id:
                continue
            if now - panel.interaction.created_at > EDIT_WINDOW:
                del self._open[key]
                continue
            drawing = render(sounds, panel.page)
            try:
                await panel.interaction.edit_original_response(
                    content=drawing.content, view=drawing.view
                )
            except discord.HTTPException:
                del self._open[key]
            else:
                panel.page = drawing.page


@dataclass
class Panels:
    """What the panel's buttons act on. The bot exposes it as `client.panels`."""

    soundboard: Soundboard
    library: SoundLibrary
    registry: PanelRegistry = field(default_factory=PanelRegistry)

    async def open(self, interaction: discord.Interaction) -> None:
        guild, _ = invoking_member(interaction)
        drawing = render(self.library.sounds(guild.id), 0)
        if drawing.view is None:
            await interaction.response.send_message(drawing.content, ephemeral=True)
        else:
            await interaction.response.send_message(
                drawing.content, view=drawing.view, ephemeral=True
            )
        self.registry.track(interaction, drawing.page)

    async def play(self, interaction: discord.Interaction, page: int, name: str) -> None:
        caller = invoking_caller(interaction)
        try:
            prepared = await self.soundboard.prepare(caller, name)
        except SoundNotFound:
            await self._redraw(interaction, caller.guild_id, page)
            raise UserError(f"`{name}` has been renamed or removed.") from None
        if prepared.must_connect:
            # Joining can take longer than the 3 s Discord allows: acknowledge silently now,
            # redraw once done.
            await interaction.response.defer()
        await self.soundboard.play(prepared)
        # Answering with a redraw costs the same single request as a plain acknowledgement,
        # and picks up any sound added or removed since the panel was last drawn.
        await self._redraw(interaction, caller.guild_id, page)

    async def turn_page(self, interaction: discord.Interaction, page: int) -> None:
        guild, _ = invoking_member(interaction)
        await self._redraw(interaction, guild.id, page)

    async def stop(self, interaction: discord.Interaction, page: int) -> None:
        caller = invoking_caller(interaction)
        if not await self.soundboard.stop(caller):
            raise UserError("Nothing was playing.")
        await self._redraw(interaction, caller.guild_id, page)

    async def refresh(self, guild_id: int) -> None:
        """Redraw the open panels of a guild, after its sounds changed."""
        await self.registry.refresh(guild_id, self.library.sounds(guild_id))

    async def follow(self, events: EventHub) -> None:
        """Refresh open panels whenever sounds change, from any source. Runs until cancelled."""
        with events.subscribe() as subscription:
            while True:
                for event in await subscription.next():
                    if event.topic is not Topic.SOUNDS:
                        continue
                    try:
                        await self.refresh(event.guild_id)
                    except Exception:
                        _log.exception("Could not refresh the panels of guild %s", event.guild_id)

    async def _redraw(self, interaction: discord.Interaction, guild_id: int, page: int) -> None:
        drawing = render(self.library.sounds(guild_id), page)
        if interaction.response.is_done():  # acknowledged already, before a slow join
            await interaction.edit_original_response(content=drawing.content, view=drawing.view)
        else:
            await interaction.response.edit_message(content=drawing.content, view=drawing.view)
        self.registry.track(interaction, drawing.page)


class _HasPanels(Protocol):
    panels: Panels


def _panels(interaction: discord.Interaction) -> Panels:
    # Typed through a Protocol so this module needn't import the bot (a circular import).
    return cast(_HasPanels, interaction.client).panels


async def _handle(interaction: discord.Interaction, action: Awaitable[None]) -> None:
    # Errors in DynamicItem callbacks are only logged by discord.py, never shown: report
    # them here the way the slash commands do.
    try:
        await action
    except Exception as error:
        await report_error(interaction, error, source="Panel button")


class PlayButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"minie:play:(?P<page>\d+):(?P<name>[a-z0-9_-]{1,32})",
):
    def __init__(self, page: int, sound_name: str, *, row: int | None = None) -> None:
        super().__init__(
            discord.ui.Button(
                label=sound_name,
                style=discord.ButtonStyle.primary,
                custom_id=f"minie:play:{page}:{sound_name}",
            ),
            row=row,
        )
        self.page = page
        self.sound_name = sound_name

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        return cls(int(match["page"]), match["name"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle(
            interaction, _panels(interaction).play(interaction, self.page, self.sound_name)
        )


class PageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    # The direction keeps the two IDs distinct when both point at the same page.
    template=r"minie:page:(?P<page>\d+):(?P<direction>previous|next)",
):
    def __init__(self, page: int, direction: str, *, disabled: bool = False) -> None:
        # A disabled edge button keeps a valid target, since IDs can't hold negative pages.
        target = max(page, 0)
        super().__init__(
            discord.ui.Button(
                label="◀" if direction == "previous" else "▶",
                style=discord.ButtonStyle.secondary,
                custom_id=f"minie:page:{target}:{direction}",
                disabled=disabled,
            ),
            row=4,
        )
        self.page = target
        self.direction = direction

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        return cls(int(match["page"]), match["direction"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle(interaction, _panels(interaction).turn_page(interaction, self.page))


class StopButton(discord.ui.DynamicItem[discord.ui.Button], template=r"minie:stop:(?P<page>\d+)"):
    def __init__(self, page: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id=f"minie:stop:{page}"
            ),
            row=4,
        )
        self.page = page

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        return cls(int(match["page"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle(interaction, _panels(interaction).stop(interaction, self.page))


BUTTONS = (PlayButton, PageButton, StopButton)
"""To register with `Client.add_dynamic_items`, so old panels work after a restart."""


def register(tree: app_commands.CommandTree, panels: Panels) -> None:
    @tree.command(name="panel", description="Open your own soundboard panel")
    async def panel(interaction: discord.Interaction) -> None:
        await panels.open(interaction)
