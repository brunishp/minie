import asyncio

import pytest

from minie.errors import UserError
from minie.playback import Overlap, PlaybackService
from minie.voice.base import NotConnectedError, OpusFrames, VoiceError

GUILD = 1
HERE, ELSEWHERE = 2, 3  # voice channels
ALICE, BOB, CAROL = 10, 11, 12


class FakeVoice:
    """A VoicePlayer whose sounds play until the test calls `finish`."""

    def __init__(self, channel: int | None = HERE) -> None:
        self.channel = channel
        self.joins: list[int] = []
        self.join_error: VoiceError | None = None
        self.started: list[list[bytes]] = []
        self.current: asyncio.Future[None] | None = None

    def connected_channel_id(self, guild_id: int) -> int | None:
        return self.channel

    def is_playing(self, guild_id: int) -> bool:
        return self.current is not None and not self.current.done()

    async def join(self, guild_id: int, channel_id: int) -> None:
        if self.join_error is not None:
            raise self.join_error
        self.joins.append(channel_id)
        self.channel = channel_id

    async def leave(self, guild_id: int) -> None:
        self.channel = None

    async def play(self, guild_id: int, frames: OpusFrames) -> asyncio.Future[None]:
        if self.channel is None:
            raise NotConnectedError("not connected")
        if self.is_playing(guild_id):
            raise VoiceError("already playing")
        self.started.append(list(frames))
        self.current = asyncio.get_running_loop().create_future()
        return self.current

    async def stop(self, guild_id: int) -> None:
        if self.is_playing(guild_id):
            self.finish()

    def finish(self) -> None:
        assert self.current is not None
        self.current.set_result(None)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def service(
    voice: FakeVoice,
    overlap: Overlap = Overlap.INTERRUPT,
    *,
    queue_max: int = 5,
    cooldown: float = 0,
    clock: Clock | None = None,
) -> PlaybackService:
    return PlaybackService(
        voice, overlap=overlap, queue_max=queue_max, cooldown=cooldown, clock=clock or Clock()
    )


async def settle() -> None:
    """Let the queue worker run until it waits again."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_interrupt_cuts_off_the_current_sound() -> None:
    voice = FakeVoice()
    playback = service(voice)
    assert await playback.play(GUILD, ALICE, HERE, [b"a"]) == 0
    first = voice.current
    assert await playback.play(GUILD, BOB, HERE, [b"b"]) == 0
    assert first is not None and first.done()
    assert voice.started == [[b"a"], [b"b"]]


async def test_queue_plays_sounds_in_order() -> None:
    voice = FakeVoice()
    playback = service(voice, Overlap.QUEUE)
    assert await playback.play(GUILD, ALICE, HERE, [b"a"]) == 0
    assert await playback.play(GUILD, BOB, HERE, [b"b"]) == 1
    assert await playback.play(GUILD, CAROL, HERE, [b"c"]) == 2
    await settle()
    assert voice.started == [[b"a"]]
    for expected in ([b"a"], [b"b"]), ([b"a"], [b"b"], [b"c"]):
        voice.finish()
        await settle()
        assert voice.started == list(expected)
    voice.finish()
    await settle()
    # Once the queue has drained, the next sound plays right away again.
    assert await playback.play(GUILD, ALICE, HERE, [b"d"]) == 0


async def test_queue_length_is_capped() -> None:
    voice = FakeVoice()
    playback = service(voice, Overlap.QUEUE, queue_max=1)
    assert await playback.play(GUILD, ALICE, HERE, [b"a"]) == 0
    assert await playback.play(GUILD, BOB, HERE, [b"b"]) == 1
    with pytest.raises(UserError, match="queue is full"):
        await playback.play(GUILD, CAROL, HERE, [b"c"])


async def test_cooldown_is_per_user() -> None:
    clock = Clock()
    playback = service(FakeVoice(), cooldown=3, clock=clock)
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    clock.now = 1.0
    with pytest.raises(UserError, match=r"in 2\.0 s"):
        await playback.play(GUILD, ALICE, HERE, [b"b"])
    await playback.play(GUILD, BOB, HERE, [b"c"])
    clock.now = 3.0
    await playback.play(GUILD, ALICE, HERE, [b"d"])


async def test_stop_also_drops_the_queue() -> None:
    voice = FakeVoice()
    playback = service(voice, Overlap.QUEUE)
    for user, frame in ((ALICE, b"a"), (BOB, b"b"), (CAROL, b"c")):
        await playback.play(GUILD, user, HERE, [frame])
    await settle()
    assert await playback.stop(GUILD) is True
    await settle()
    assert voice.started == [[b"a"]]
    assert await playback.stop(GUILD) is False


@pytest.mark.parametrize("overlap", list(Overlap))
async def test_joins_the_members_channel_when_in_none(overlap: Overlap) -> None:
    voice = FakeVoice(channel=None)
    assert await service(voice, overlap).play(GUILD, ALICE, HERE, [b"a"]) == 0
    await settle()
    assert voice.joins == [HERE]
    assert voice.started == [[b"a"]]


async def test_moves_from_a_quiet_channel() -> None:
    voice = FakeVoice(channel=ELSEWHERE)
    await service(voice).play(GUILD, ALICE, HERE, [b"a"])
    assert voice.joins == [HERE]


@pytest.mark.parametrize("overlap", list(Overlap))
async def test_stays_with_the_channel_it_plays_for(overlap: Overlap) -> None:
    voice = FakeVoice(channel=ELSEWHERE)
    playback = service(voice, overlap)
    await playback.play(GUILD, ALICE, ELSEWHERE, [b"a"])
    await settle()
    with pytest.raises(UserError, match="busy"):
        await playback.play(GUILD, BOB, HERE, [b"b"])
    with pytest.raises(UserError, match="busy"):
        await playback.join(GUILD, HERE)  # /join follows the same rule
    assert voice.joins == []
    assert voice.started == [[b"a"]]

    voice.finish()  # quiet again: now it may move
    await settle()
    await playback.play(GUILD, BOB, HERE, [b"b"])
    assert voice.joins == [HERE]


async def test_a_queue_waiting_counts_as_busy() -> None:
    voice = FakeVoice(channel=ELSEWHERE)
    playback = service(voice, Overlap.QUEUE)
    await playback.play(GUILD, ALICE, ELSEWHERE, [b"a"])
    await playback.play(GUILD, CAROL, ELSEWHERE, [b"c"])
    await settle()
    voice.finish()  # "a" ends, "c" is about to start
    with pytest.raises(UserError, match="busy"):
        await playback.play(GUILD, BOB, HERE, [b"b"])


async def test_a_failed_join_is_reported_and_plays_nothing() -> None:
    voice = FakeVoice(channel=None)
    voice.join_error = VoiceError("Couldn't connect to the voice channel.")
    with pytest.raises(VoiceError, match="Couldn't connect"):
        await service(voice).play(GUILD, ALICE, HERE, [b"a"])
    assert voice.started == []
