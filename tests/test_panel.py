from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from minie.commands.panel import (
    BUTTONS,
    PAGE_SIZE,
    PageButton,
    PanelRegistry,
    PlayButton,
    StopButton,
    render,
)
from minie.store import Sound

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def sounds(count: int) -> list[Sound]:
    return [Sound(f"id{i}", 1, f"sound-{i:03d}", 42, 1500, NOW) for i in range(count)]


def buttons(view: discord.ui.View | None) -> list[discord.ui.DynamicItem[discord.ui.Button]]:
    assert view is not None
    return [cast(discord.ui.DynamicItem[discord.ui.Button], item) for item in view.children]


def plays(view: discord.ui.View | None) -> list[str]:
    return [item.sound_name for item in buttons(view) if isinstance(item, PlayButton)]


def edge_buttons(view: discord.ui.View | None) -> tuple[bool, bool]:
    """Whether the previous and next buttons are disabled."""
    previous, following = (item.item for item in buttons(view) if isinstance(item, PageButton))
    return previous.disabled, following.disabled


def test_an_empty_library_has_no_buttons() -> None:
    drawing = render([], 3)
    assert drawing.view is None
    assert drawing.page == 0
    assert "No sounds yet" in drawing.content


def test_one_full_page() -> None:
    drawing = render(sounds(PAGE_SIZE), 0)
    assert plays(drawing.view) == [f"sound-{i:03d}" for i in range(PAGE_SIZE)]
    assert len(buttons(drawing.view)) == 23  # 20 sounds, previous, next, stop
    assert edge_buttons(drawing.view) == (True, True)
    assert "page 1/1 · 20 sounds" in drawing.content


def test_pages_and_clamping() -> None:
    library = sounds(45)
    middle = render(library, 1)
    assert plays(middle.view)[0] == "sound-020"
    assert edge_buttons(middle.view) == (False, False)
    last = render(library, 2)
    assert len(plays(last.view)) == 5
    assert edge_buttons(last.view) == (False, True)
    beyond = render(library, 9)  # the library shrank since this page was drawn
    assert beyond.page == 2
    assert "page 3/3" in beyond.content


@pytest.mark.parametrize("count", [1, 20, 45])
async def test_custom_ids_are_unique_and_round_trip(count: int) -> None:
    drawing = render(sounds(count), 1)
    ids = [item.custom_id for item in buttons(drawing.view)]
    assert len(ids) == len(set(ids))
    for item in buttons(drawing.view):
        match = type(item).__discord_ui_compiled_template__.fullmatch(item.custom_id)
        assert match is not None, item.custom_id
        rebuilt = await type(item).from_custom_id(cast(discord.Interaction, None), item.item, match)
        assert rebuilt.custom_id == item.custom_id
        assert getattr(rebuilt, "page", None) == getattr(item, "page", None)
    assert {type(item) for item in buttons(drawing.view)} == set(BUTTONS)


def test_stop_and_page_buttons_share_the_last_row() -> None:
    drawing = render(sounds(PAGE_SIZE), 0)
    controls = [item for item in buttons(drawing.view) if isinstance(item, PageButton | StopButton)]
    assert {item.row for item in controls} == {4}


class FakeInteraction:
    """The part of an Interaction that PanelRegistry uses."""

    def __init__(self, user_id: int, *, age: timedelta = timedelta(), fail: bool = False) -> None:
        self.guild_id = 1
        self.user = SimpleNamespace(id=user_id)
        self.created_at = NOW - age
        self.fail = fail
        self.edits: list[str] = []

    async def edit_original_response(self, *, content: str, view: discord.ui.View | None) -> None:
        if self.fail:  # e.g. the member dismissed their panel
            response = cast(Any, SimpleNamespace(status=404, reason="Not Found"))
            raise discord.NotFound(response, "Unknown Message")
        self.edits.append(content)


def track(registry: PanelRegistry, interaction: FakeInteraction, page: int) -> None:
    registry.track(cast(discord.Interaction, interaction), page)


async def test_refresh_redraws_each_open_panel_at_its_own_page() -> None:
    registry = PanelRegistry(clock=lambda: NOW)
    alice, bob = FakeInteraction(10), FakeInteraction(11)
    track(registry, alice, 0)
    track(registry, bob, 2)
    await registry.refresh(1, sounds(45))
    assert alice.edits == [render(sounds(45), 0).content]
    assert bob.edits == [render(sounds(45), 2).content]
    await registry.refresh(2, sounds(45))  # another guild: nothing to do
    assert len(alice.edits) == 1


async def test_a_newer_interaction_replaces_the_older_one() -> None:
    registry = PanelRegistry(clock=lambda: NOW)
    old, new = FakeInteraction(10), FakeInteraction(10)
    track(registry, old, 0)
    track(registry, new, 0)
    await registry.refresh(1, sounds(3))
    assert (old.edits, len(new.edits)) == ([], 1)


async def test_expired_and_failed_panels_are_dropped() -> None:
    registry = PanelRegistry(clock=lambda: NOW)
    stale = FakeInteraction(10, age=timedelta(minutes=14, seconds=1))
    dismissed = FakeInteraction(11, fail=True)
    track(registry, stale, 0)
    track(registry, dismissed, 0)
    await registry.refresh(1, sounds(3))
    dismissed.fail = False
    await registry.refresh(1, sounds(3))
    assert stale.edits == []
    assert dismissed.edits == []  # not retried once it failed


async def test_refresh_follows_a_shrinking_library() -> None:
    registry = PanelRegistry(clock=lambda: NOW)
    member = FakeInteraction(10)
    track(registry, member, 2)
    await registry.refresh(1, sounds(5))
    await registry.refresh(1, sounds(45))
    # The first redraw clamped the panel to page 1, which it keeps afterwards.
    assert member.edits == [render(sounds(5), 0).content, render(sounds(45), 0).content]
