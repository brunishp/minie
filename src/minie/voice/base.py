"""The seam between the bot and whatever actually speaks Discord's voice protocol.

The rest of the bot talks to a `VoicePlayer` with plain IDs and Opus frames. The discord.py
implementation can then be swapped for another one (for instance a hand-written voice
gateway, UDP/RTP transport and DAVE layer) without touching commands or storage.
"""

import asyncio
from collections.abc import Iterable
from typing import Protocol

from minie.errors import UserError

FRAME_DURATION_MS = 20
"""Every Opus packet handed to a `VoicePlayer` covers exactly this much audio.

Discord voice senders emit one packet per 20 ms tick and advance the RTP timestamp by
960 samples (20 ms at 48 kHz) each time, so longer or shorter packets would play at the
wrong speed or overrun the receivers' jitter buffers.
"""

# 20 ms Opus packets in playback order. A player may consume them from its own sender
# thread, so the iterable must not depend on the asyncio event loop.
type OpusFrames = Iterable[bytes]


class VoiceError(UserError):
    """A voice operation failed in a way worth telling the user about."""


class NotConnectedError(VoiceError):
    pass


class VoicePlayer(Protocol):
    def connected_channel_id(self, guild_id: int) -> int | None:
        """The voice channel the bot is connected to in this guild, if any."""
        ...

    def is_playing(self, guild_id: int) -> bool: ...

    async def join(self, guild_id: int, channel_id: int) -> None:
        """Connect to a voice channel, or move there if already connected elsewhere.

        Returns once audio can be sent, which includes the DAVE handshake.
        """
        ...

    async def leave(self, guild_id: int) -> None:
        """Disconnect from voice in this guild. Does nothing when not connected."""
        ...

    async def play(self, guild_id: int, frames: OpusFrames) -> asyncio.Future[None]:
        """Start sending `frames` and return a future for the end of playback.

        The future completes when the frames run out or `stop` is called, or fails with
        the error that interrupted playback. Raises `NotConnectedError` when not in a
        voice channel, and `VoiceError` when something is already playing.
        """
        ...

    async def stop(self, guild_id: int) -> None:
        """Stop the current playback and return once it has actually ended."""
        ...
