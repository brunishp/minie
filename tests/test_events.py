import asyncio

import pytest

from minie.events import Event, EventHub, Topic


async def test_delivers_a_guilds_events_once_each() -> None:
    hub = EventHub()
    with hub.subscribe(1) as one, hub.subscribe() as every:
        for guild_id, topic in ((1, Topic.SOUNDS), (2, Topic.STATUS), (1, Topic.SOUNDS)):
            hub.publish(guild_id, topic)
        assert await one.next() == [Event(1, Topic.SOUNDS)]  # merged, other guild left out
        assert await every.next() == [Event(1, Topic.SOUNDS), Event(2, Topic.STATUS)]


async def test_waits_for_the_next_change() -> None:
    hub = EventHub()
    with hub.subscribe(1) as subscription:
        waiting = asyncio.create_task(subscription.next())
        await asyncio.sleep(0)
        assert not waiting.done()
        hub.publish(1, Topic.STATUS)
        assert await waiting == [Event(1, Topic.STATUS)]


async def test_a_timed_out_wait_keeps_later_events() -> None:
    hub = EventHub()
    with hub.subscribe(1) as subscription:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await subscription.next()
        hub.publish(1, Topic.SOUNDS)
        assert await subscription.next() == [Event(1, Topic.SOUNDS)]


async def test_unsubscribes_on_exit() -> None:
    hub = EventHub()
    with hub.subscribe() as subscription:
        pass
    hub.publish(1, Topic.SOUNDS)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await subscription.next()
