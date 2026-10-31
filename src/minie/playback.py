"""What happens when someone triggers a sound: the cooldown, then interrupt or queue.

These rules sit above `VoicePlayer`, so they stay the same whichever voice implementation
is plugged in, and whether the trigger is a slash command or, later, the web UI.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from minie.errors import UserError
from minie.voice.base import NotConnectedError, OpusFrames, VoiceError, VoicePlayer

_log = logging.getLogger(__name__)


class Overlap(StrEnum):
    """What a new sound does while another one is playing."""

    INTERRUPT = "interrupt"
    """Cut the current sound off. Feels immediate, and spamming can't build up a backlog."""
    QUEUE = "queue"
    """Play after the current sound and any already waiting, up to a maximum queue length."""


@dataclass
class _Guild:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue: deque[OpusFrames] = field(default_factory=deque)
    worker: asyncio.Task[None] | None = None


class PlaybackService:
    def __init__(
        self,
        voice: VoicePlayer,
        *,
        overlap: Overlap,
        queue_max: int,
        cooldown: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._voice = voice
        self._overlap = overlap
        self._queue_max = queue_max
        self._cooldown = cooldown
        self._clock = clock
        self._guilds: dict[int, _Guild] = {}
        self._last_played: dict[tuple[int, int], float] = {}

    async def play(self, guild_id: int, user_id: int, frames: OpusFrames) -> int:
        """Play `frames` now or queue them. Returns the queue position, 0 meaning now."""
        if self._voice.connected_channel_id(guild_id) is None:
            raise NotConnectedError("I'm not in a voice channel, use /join first.")
        self._check_cooldown(guild_id, user_id)
        state = self._guilds.setdefault(guild_id, _Guild())
        async with state.lock:
            if self._overlap is Overlap.INTERRUPT:
                await self._voice.stop(guild_id)
                await self._voice.play(guild_id, frames)
                return 0
            return self._enqueue(guild_id, state, frames)

    async def stop(self, guild_id: int) -> bool:
        """Stop the current sound and drop the queue. Returns whether there was anything."""
        state = self._guilds.setdefault(guild_id, _Guild())
        async with state.lock:
            had_something = bool(state.queue) or self._voice.is_playing(guild_id)
            state.queue.clear()
            await self._voice.stop(guild_id)
        return had_something

    def _check_cooldown(self, guild_id: int, user_id: int) -> None:
        now = self._clock()
        last = self._last_played.get((guild_id, user_id))
        if last is not None and (remaining := self._cooldown - (now - last)) > 0:
            raise UserError(f"Slow down: you can play another sound in {remaining:.1f} s.")
        self._last_played[(guild_id, user_id)] = now

    def _enqueue(self, guild_id: int, state: _Guild, frames: OpusFrames) -> int:
        if state.worker is None:
            # Handed to the worker directly: the queue only ever holds waiting sounds, even
            # before the new task first runs.
            state.worker = asyncio.create_task(self._drain(guild_id, state, frames))
            return 0
        if len(state.queue) >= self._queue_max:
            raise UserError("The queue is full, try again in a moment.")
        state.queue.append(frames)
        return len(state.queue)

    async def _drain(self, guild_id: int, state: _Guild, frames: OpusFrames) -> None:
        try:
            while True:
                try:
                    playback = await self._voice.play(guild_id, frames)
                except VoiceError as exc:
                    _log.warning("Dropping the queue in guild %s: %s", guild_id, exc)
                    state.queue.clear()
                    return
                # Wait without re-raising: the voice player logs playback failures itself.
                await asyncio.wait({playback})
                # No await between this check and `worker = None` below, so a sound queued
                # meanwhile is either picked up here or starts a new worker.
                if not state.queue:
                    return
                frames = state.queue.popleft()
        finally:
            state.worker = None
