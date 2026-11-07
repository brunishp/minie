import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from fakes import STEREO_20MS, Clock, FakePresence, FakeVoice, write_opus

from minie.config import Settings
from minie.control import (
    COMMAND_PERMISSIONS_TTL,
    WHOLE_APP,
    Caller,
    DiscordPresence,
    NotAllowed,
    Soundboard,
)
from minie.errors import UserError
from minie.events import Event, EventHub, Topic
from minie.library import SoundLibrary, SoundNotFound
from minie.playback import Overlap, PlaybackService
from minie.store import Sound, SoundStore

GUILD = 1
APP = 4242
HERE, ELSEWHERE = 2, 3
ALICE, BOB, ADMIN = 10, 11, 12
TONE = [STEREO_20MS + b"tone"]
HORN = [STEREO_20MS + b"horn"]
SPOKEN = [STEREO_20MS + b"spoken"]


class World:
    def __init__(self, tmp_path: Path, store: SoundStore) -> None:
        self.tmp_path = tmp_path
        self.store = store
        self.events = EventHub()
        self.voice = FakeVoice()
        self.presence = FakePresence()
        self.library = SoundLibrary(store, Settings(data_dir=tmp_path), self.events)
        playback = PlaybackService(
            self.voice, overlap=Overlap.INTERRUPT, queue_max=5, cooldown=0, events=self.events
        )
        self.soundboard = Soundboard(
            voice=self.voice,
            playback=playback,
            library=self.library,
            presence=self.presence,
            test_tone=write_opus(tmp_path / "tone.ogg", TONE),
        )

    def sound(self, name: str, uploader: int = ALICE) -> Sound:
        sound = Sound(f"{name}-id", GUILD, name, uploader, 20, datetime(2026, 11, 1, tzinfo=UTC))
        self.store.add(sound, max_per_guild=10)
        write_opus(self.tmp_path / "sounds" / str(GUILD) / f"{sound.id}.ogg", HORN)
        return sound


def member(user_id: int, channel: int | None = HERE, **extra: bool) -> Caller:
    return Caller(guild_id=GUILD, user_id=user_id, voice_channel_id=channel, **extra)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SoundStore]:
    store = SoundStore(tmp_path / "minie.db")
    yield store
    store.close()


@pytest.fixture
def world(tmp_path: Path, store: SoundStore) -> World:
    return World(tmp_path, store)


def test_target_channel(world: World) -> None:
    board = world.soundboard
    with pytest.raises(UserError, match="Join a voice channel first"):
        board.target_channel(member(ALICE, None))
    with pytest.raises(UserError, match="Stage channels"):
        board.target_channel(member(ALICE, None, in_stage=True))
    assert board.target_channel(member(ALICE)) == (HERE, True)
    world.voice.channel = HERE
    assert board.target_channel(member(ALICE)) == (HERE, False)


def test_permissions_are_checked_before_connecting(world: World) -> None:
    world.presence.unjoinable[HERE] = "I'm missing permissions in <#2>: Speak."
    with pytest.raises(UserError, match="Speak"):
        world.soundboard.target_channel(member(ALICE))
    world.voice.channel = HERE  # already there: nothing left to check
    assert world.soundboard.target_channel(member(ALICE)) == (HERE, False)


async def test_play_joins_and_plays(world: World) -> None:
    world.sound("horn")
    prepared = await world.soundboard.prepare(member(ALICE), "HORN")
    assert prepared.must_connect
    assert prepared.sound is not None and prepared.sound.name == "horn"
    assert await world.soundboard.play(prepared) == 0
    assert world.voice.joins == [HERE]
    assert world.voice.started == [HORN]


async def test_test_tone(world: World) -> None:
    prepared = await world.soundboard.prepare(member(ALICE), None)
    assert prepared.sound is None
    await world.soundboard.play(prepared)
    assert world.voice.started == [TONE]


async def test_an_unknown_sound_is_reported_as_such(world: World) -> None:
    with pytest.raises(SoundNotFound):
        await world.soundboard.prepare(member(ALICE), "nothing")


async def test_stop_needs_the_bots_channel(world: World) -> None:
    with pytest.raises(UserError, match="not in a voice channel"):
        await world.soundboard.stop(member(ALICE))
    world.voice.channel = HERE
    with pytest.raises(UserError, match="Join <#2> first"):
        await world.soundboard.stop(member(ALICE, ELSEWHERE))
    assert await world.soundboard.stop(member(ALICE)) is False


async def test_join_and_leave(world: World) -> None:
    assert await world.soundboard.join(member(ALICE)) == HERE
    assert await world.soundboard.join(member(ALICE)) == HERE  # already there
    assert world.voice.joins == [HERE]
    assert await world.soundboard.leave(member(ALICE)) is True
    assert world.voice.channel is None


async def test_leaving_is_for_those_in_the_channel_or_a_manager(world: World) -> None:
    board = world.soundboard
    assert await board.leave(member(BOB, None)) is False  # in no channel: anyone may clear it
    await board.join(member(ALICE))
    for outsider in (member(BOB, ELSEWHERE), member(BOB, None)):
        assert not board.can_leave(outsider)
        with pytest.raises(NotAllowed, match="Join <#2> first"):
            await board.leave(outsider)
    assert world.voice.channel == HERE
    assert await board.leave(member(ADMIN, None, manage_guild=True)) is True
    assert world.voice.channel is None


def test_only_the_uploader_or_a_manager_may_change_a_sound(world: World) -> None:
    world.sound("horn", uploader=ALICE)
    with pytest.raises(UserError, match="Only whoever added a sound"):
        world.soundboard.rename(member(BOB), "horn", "mine")
    with pytest.raises(UserError, match="Only whoever added a sound"):
        world.soundboard.remove(member(BOB), "horn")
    before, after = world.soundboard.rename(member(ALICE), "horn", "Air_Horn")
    assert (before.name, after.name) == ("horn", "air_horn")
    removed = world.soundboard.remove(member(ADMIN, manage_guild=True), "air_horn")
    assert removed.id == before.id
    assert world.library.sounds(GUILD) == []


async def test_changes_are_published(world: World) -> None:
    world.sound("horn")
    with world.events.subscribe(GUILD) as subscription:
        world.soundboard.rename(member(ALICE), "horn", "drum")
        assert await subscription.next() == [Event(GUILD, Topic.SOUNDS)]
        world.soundboard.remove(member(ALICE), "drum")
        assert await subscription.next() == [Event(GUILD, Topic.SOUNDS)]


class FakeHttp:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.permissions: list[dict[str, Any]] = []

    async def get_global_commands(self, application_id: int) -> list[dict[str, Any]]:
        return [{"id": "100", "name": "play"}, {"id": "101", "name": "add"}]

    async def get_guild_commands(self, application_id: int, guild_id: int) -> list[dict[str, Any]]:
        return [{"id": "200", "name": "remove"}]

    async def get_guild_application_command_permissions(
        self, application_id: int, guild_id: int
    ) -> list[dict[str, Any]]:
        self.calls += 1
        if self.fail:
            raise discord.HTTPException(cast(Any, SimpleNamespace(status=500, reason="")), "")
        return self.permissions


async def test_reads_command_restrictions_from_discord() -> None:
    http = FakeHttp()
    clock = Clock()
    client = cast(discord.Client, SimpleNamespace(application_id=APP, http=http))
    presence = DiscordPresence(client, clock=clock)
    allow = [{"id": "1", "type": 1, "permission": True}]
    http.permissions = [
        {"id": "101", "permissions": allow},  # /add restricted
        {"id": "100", "permissions": []},  # /play: an entry with nothing in it
        {"id": "200", "permissions": allow},  # /remove, a guild copy
    ]
    assert await presence.restricted_commands(GUILD) == {"add", "remove"}

    http.permissions = [{"id": str(APP), "permissions": allow}]
    assert await presence.restricted_commands(GUILD) == {"add", "remove"}  # still cached
    presence.forget_restrictions(GUILD)  # Discord announced a change
    assert await presence.restricted_commands(GUILD) == {WHOLE_APP}
    clock.now += COMMAND_PERMISSIONS_TTL
    http.fail = True
    with pytest.raises(UserError, match="couldn't check"):
        await presence.restricted_commands(GUILD)


@pytest.mark.parametrize(
    ("viewers", "name"),
    [
        ({"member", "bot"}, "lounge"),
        ({"bot"}, None),  # the member can't see it
        ({"member"}, None),  # the bot can't, so Discord only sent it a placeholder name
    ],
)
async def test_names_a_channel_only_when_both_can_view_it(
    viewers: set[str], name: str | None
) -> None:
    channel = SimpleNamespace(
        name="lounge",
        permissions_for=lambda who: SimpleNamespace(view_channel=who in viewers),
    )
    guild = SimpleNamespace(get_channel=lambda _: channel, get_member=lambda _: "member", me="bot")
    client = cast(discord.Client, SimpleNamespace(get_guild=lambda _: guild))
    assert await DiscordPresence(client).visible_channel_name(member(ALICE), HERE) == name


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def frames(self, text: str) -> list[bytes]:
        self.said.append(text)
        return SPOKEN


def speaking_board(world: World, *, cooldown: float = 0) -> tuple[Soundboard, FakeSpeaker]:
    speaker = FakeSpeaker()
    playback = PlaybackService(
        world.voice, overlap=Overlap.INTERRUPT, queue_max=5, cooldown=cooldown, events=world.events
    )
    board = Soundboard(
        voice=world.voice,
        playback=playback,
        library=world.library,
        presence=world.presence,
        test_tone=world.tmp_path / "tone.ogg",
        speaker=speaker,
    )
    return board, speaker


async def test_say_joins_and_plays_the_speech(world: World) -> None:
    board, speaker = speaking_board(world)
    assert board.can_speak
    prepared = await board.prepare_speech(member(ALICE), "bonjour")
    assert prepared.must_connect
    assert prepared.sound is None
    assert await board.play(prepared) == 0
    assert speaker.said == ["bonjour"]
    assert world.voice.joins == [HERE]
    assert world.voice.started == [SPOKEN]


async def test_say_generates_nothing_when_refused(world: World) -> None:
    board, speaker = speaking_board(world, cooldown=3)
    with pytest.raises(UserError, match="Join a voice channel"):
        await board.prepare_speech(member(ALICE, None), "bonjour")
    with pytest.raises(UserError, match="nothing to say"):
        await board.prepare_speech(member(ALICE), "...")
    world.presence.unjoinable[HERE] = "I'm missing permissions in <#2>: Speak."
    with pytest.raises(UserError, match="Speak"):
        await board.prepare_speech(member(ALICE), "bonjour")
    world.presence.unjoinable.clear()
    await board.play(await board.prepare_speech(member(ALICE), "bonjour"))
    with pytest.raises(UserError, match="Slow down"):  # no free speech during the cooldown
        await board.prepare_speech(member(ALICE), "encore")
    assert speaker.said == ["bonjour"]


async def test_say_never_logs_the_text(world: World, caplog: pytest.LogCaptureFixture) -> None:
    board, _ = speaking_board(world)
    with caplog.at_level(logging.DEBUG, logger="minie"):
        await board.prepare_speech(member(ALICE), "un secret bien gardé")
    assert f"User {ALICE} used /say" in caplog.text
    assert "secret" not in caplog.text


def test_say_is_off_without_a_speaker(world: World) -> None:
    assert not world.soundboard.can_speak
    with pytest.raises(UserError, match="isn't set up"):
        world.soundboard.check_can_say(member(ALICE), "bonjour")
