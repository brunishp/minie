from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import STEREO_20MS, FakePresence, FakeVoice, write_opus

from minie.config import Settings
from minie.control import Caller, Soundboard
from minie.errors import UserError
from minie.events import Event, EventHub, Topic
from minie.library import SoundLibrary, SoundNotFound
from minie.playback import Overlap, PlaybackService
from minie.store import Sound, SoundStore

GUILD = 1
HERE, ELSEWHERE = 2, 3
ALICE, BOB, ADMIN = 10, 11, 12
TONE = [STEREO_20MS + b"tone"]
HORN = [STEREO_20MS + b"horn"]


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
    assert await world.soundboard.leave(member(BOB, None)) is True
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
