"""Change notifications inside the bot: the library and playback publish, panels and web pages
listen.

An event only says what changed (a guild's sounds, or its playback status); listeners read the
current state themselves. So pending events can be merged: a slow listener gets each kind of
change once however often it happened, and nothing queues up without bound.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum


class Topic(StrEnum):
    SOUNDS = "sounds"
    """The guild's sound list changed."""
    STATUS = "status"
    """Where the bot is, what it plays or what waits in the queue changed."""


@dataclass(frozen=True)
class Event:
    guild_id: int
    topic: Topic


class Subscription:
    def __init__(self, guild_id: int | None) -> None:
        self.guild_id = guild_id
        """Only this guild's events, or every guild's when None."""
        self._pending: dict[Event, None] = {}  # an ordered set
        self._ready = asyncio.Event()

    def _deliver(self, event: Event) -> None:
        if self.guild_id in (None, event.guild_id):
            self._pending[event] = None
            self._ready.set()

    async def next(self) -> list[Event]:
        """Wait for changes, then return each pending one once.

        Safe to cancel, for instance with `asyncio.timeout`: pending events stay for the
        next call.
        """
        await self._ready.wait()
        self._ready.clear()
        events = list(self._pending)
        self._pending.clear()
        return events


class EventHub:
    """Publish from the event loop thread only."""

    def __init__(self) -> None:
        self._subscriptions: set[Subscription] = set()

    def publish(self, guild_id: int, topic: Topic) -> None:
        event = Event(guild_id, topic)
        for subscription in self._subscriptions:
            subscription._deliver(event)

    @contextmanager
    def subscribe(self, guild_id: int | None = None) -> Iterator[Subscription]:
        subscription = Subscription(guild_id)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)
