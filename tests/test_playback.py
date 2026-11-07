import asyncio

import pytest
from fakes import Clock, FakeVoice

from minie.errors import UserError
from minie.events import EventHub, Topic
from minie.playback import BusyError, Overlap, PlaybackService, Status
from minie.voice.base import VoiceError

GUILD = 1
HERE, ELSEWHERE = 2, 3  # voice channels
ALICE, BOB, CAROL = 10, 11, 12


def service(
    voice: FakeVoice,
    overlap: Overlap = Overlap.INTERRUPT,
    *,
    queue_max: int = 5,
    cooldown: float = 0,
    clock: Clock | None = None,
    events: EventHub | None = None,
) -> PlaybackService:
    return PlaybackService(
        voice,
        overlap=overlap,
        queue_max=queue_max,
        cooldown=cooldown,
        clock=clock or Clock(),
        events=events,
    )


async def settle() -> None:
    """Let the queue worker run until it waits again."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_interrupt_cuts_off_the_current_sound() -> None:
    voice = FakeVoice(channel=HERE)
    playback = service(voice)
    assert await playback.play(GUILD, ALICE, HERE, [b"a"]) == 0
    first = voice.current
    assert await playback.play(GUILD, BOB, HERE, [b"b"]) == 0
    assert first is not None and first.done()
    assert voice.started == [[b"a"], [b"b"]]


async def test_queue_plays_sounds_in_order() -> None:
    voice = FakeVoice(channel=HERE)
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
    voice = FakeVoice(channel=HERE)
    playback = service(voice, Overlap.QUEUE, queue_max=1)
    assert await playback.play(GUILD, ALICE, HERE, [b"a"]) == 0
    assert await playback.play(GUILD, BOB, HERE, [b"b"]) == 1
    with pytest.raises(UserError, match="queue is full"):
        await playback.play(GUILD, CAROL, HERE, [b"c"])


async def test_cooldown_is_per_user() -> None:
    clock = Clock()
    playback = service(FakeVoice(channel=HERE), cooldown=3, clock=clock)
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    clock.now = 1.0
    with pytest.raises(UserError, match=r"in 2\.0 s"):
        await playback.play(GUILD, ALICE, HERE, [b"b"])
    await playback.play(GUILD, BOB, HERE, [b"c"])
    clock.now = 3.0
    await playback.play(GUILD, ALICE, HERE, [b"d"])


async def test_stop_also_drops_the_queue() -> None:
    voice = FakeVoice(channel=HERE)
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


async def test_status_and_leave() -> None:
    voice = FakeVoice(channel=None)
    playback = service(voice, Overlap.QUEUE)
    assert playback.status(GUILD) == Status(channel_id=None, playing=False, queued=0)
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    await playback.play(GUILD, BOB, HERE, [b"b"])
    await settle()
    assert playback.status(GUILD) == Status(channel_id=HERE, playing=True, queued=1)
    assert await playback.leave(GUILD) is True
    await settle()
    assert playback.status(GUILD) == Status(channel_id=None, playing=False, queued=0)
    assert voice.started == [[b"a"]]
    assert await playback.leave(GUILD) is False


@pytest.mark.parametrize("overlap", list(Overlap))
async def test_publishes_status_changes(overlap: Overlap) -> None:
    events = EventHub()
    voice = FakeVoice(channel=None)
    playback = service(voice, overlap, events=events)
    with events.subscribe(GUILD) as subscription:
        await playback.play(GUILD, ALICE, HERE, [b"a"])  # joins, then starts
        await settle()
        assert [event.topic for event in await subscription.next()] == [Topic.STATUS]
        voice.finish()  # the sound ends on its own
        await settle()
        assert [event.topic for event in await subscription.next()] == [Topic.STATUS]
        await playback.stop(GUILD)
        assert [event.topic for event in await subscription.next()] == [Topic.STATUS]


async def test_a_busy_refusal_costs_no_cooldown() -> None:
    voice = FakeVoice(channel=ELSEWHERE)
    playback = service(voice, cooldown=3)
    await playback.play(GUILD, ALICE, ELSEWHERE, [b"a"])
    with pytest.raises(BusyError):
        await playback.play(GUILD, BOB, HERE, [b"b"])
    voice.finish()
    await settle()
    await playback.play(GUILD, BOB, HERE, [b"b"])  # at once, with no "Slow down"
    assert voice.joins == [HERE]


async def test_a_failed_join_costs_no_cooldown() -> None:
    voice = FakeVoice(channel=None)
    playback = service(voice, cooldown=3)
    voice.join_error = VoiceError("Couldn't connect to the voice channel.")
    with pytest.raises(VoiceError):
        await playback.play(GUILD, ALICE, HERE, [b"a"])
    voice.join_error = None
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    assert voice.started == [[b"a"]]


async def test_a_full_queue_costs_no_cooldown() -> None:
    voice = FakeVoice(channel=HERE)
    playback = service(voice, Overlap.QUEUE, queue_max=1, cooldown=3)
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    await playback.play(GUILD, BOB, HERE, [b"b"])
    with pytest.raises(UserError, match="queue is full"):
        await playback.play(GUILD, CAROL, HERE, [b"c"])
    await settle()
    voice.finish()  # "b" starts, so the queue has room again
    await settle()
    assert await playback.play(GUILD, CAROL, HERE, [b"c"]) == 1


async def test_a_refusal_keeps_a_cooldown_already_running() -> None:
    clock = Clock()
    voice = FakeVoice(channel=HERE)
    playback = service(voice, cooldown=3, clock=clock)
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    with pytest.raises(UserError, match="Slow down"):
        await playback.play(GUILD, ALICE, HERE, [b"b"])
    clock.now = 3.0
    await playback.play(GUILD, ALICE, HERE, [b"b"])  # counted from the first sound


async def test_a_double_click_during_a_slow_connect_plays_once() -> None:
    class SlowVoice(FakeVoice):
        def __init__(self) -> None:
            super().__init__(channel=None)
            self.connected = asyncio.Event()

        async def join(self, guild_id: int, channel_id: int) -> None:
            await self.connected.wait()
            await super().join(guild_id, channel_id)

    voice = SlowVoice()
    playback = service(voice, cooldown=3)
    first = asyncio.create_task(playback.play(GUILD, ALICE, HERE, [b"a"]))
    await settle()
    with pytest.raises(UserError, match="Slow down"):  # right away, without waiting for the join
        await playback.play(GUILD, ALICE, HERE, [b"b"])
    voice.connected.set()
    assert await first == 0
    assert voice.started == [[b"a"]]


async def test_checking_the_cooldown_does_not_start_it() -> None:
    clock = Clock()
    playback = service(FakeVoice(channel=HERE), cooldown=3, clock=clock)
    playback.check_cooldown(GUILD, ALICE)
    playback.check_cooldown(GUILD, ALICE)
    await playback.play(GUILD, ALICE, HERE, [b"a"])
    with pytest.raises(UserError, match="Slow down"):
        playback.check_cooldown(GUILD, ALICE)
    clock.now = 3.0
    playback.check_cooldown(GUILD, ALICE)
