"""`VoicePlayer` backed by discord.py's voice client (voice gateway v8, UDP, DAVE via davey)."""

import asyncio
import contextlib
import logging
from collections.abc import Iterator

import discord

from minie.voice.base import NotConnectedError, OpusFrames, VoiceError

_log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 20.0
# After a stop, discord.py's sender thread finishes its current 20 ms tick and sends five
# frames of silence before calling `after`. This is a generous upper bound for that.
STOP_TIMEOUT = 2.0


class _OpusFrameSource(discord.AudioSource):
    """Hands pre-encoded frames to discord.py's sender thread, one per 20 ms tick."""

    def __init__(self, frames: OpusFrames) -> None:
        self._frames: Iterator[bytes] = iter(frames)

    def read(self) -> bytes:
        return next(self._frames, b"")

    def is_opus(self) -> bool:
        return True


class DiscordPyVoicePlayer:
    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self._locks: dict[int, asyncio.Lock] = {}
        self._playbacks: dict[int, asyncio.Future[None]] = {}

    def connected_channel_id(self, guild_id: int) -> int | None:
        voice_client = self._voice_client(guild_id)
        if voice_client is None or not voice_client.is_connected():
            return None
        return voice_client.channel.id

    def is_playing(self, guild_id: int) -> bool:
        playback = self._playbacks.get(guild_id)
        return playback is not None and not playback.done()

    async def join(self, guild_id: int, channel_id: int) -> None:
        channel = self._client.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel) or channel.guild.id != guild_id:
            raise VoiceError("That voice channel isn't available.")

        # One connection change at a time per guild: discord.py refuses a second connect()
        # while the first one is still negotiating.
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            voice_client = self._voice_client(guild_id)
            if voice_client is not None and not voice_client.is_connected():
                # Left over from a failed attempt or a reconnect that gave up.
                await voice_client.disconnect(force=True)
                voice_client = None
            try:
                if voice_client is None:
                    # Self-deafened: the bot never listens, and users can see that it doesn't.
                    await channel.connect(timeout=CONNECT_TIMEOUT, self_deaf=True)
                elif voice_client.channel.id != channel_id:
                    await voice_client.move_to(channel, timeout=CONNECT_TIMEOUT)
            except Exception as exc:
                _log.exception("Voice connection to channel %s failed", channel_id)
                await self._drop_half_open(guild_id)
                raise VoiceError(
                    "Couldn't connect to the voice channel, see the bot's logs."
                ) from exc

    async def leave(self, guild_id: int) -> None:
        async with self._locks.setdefault(guild_id, asyncio.Lock()):
            voice_client = self._voice_client(guild_id)
            if voice_client is not None:
                await voice_client.disconnect(force=True)

    async def play(self, guild_id: int, frames: OpusFrames) -> asyncio.Future[None]:
        voice_client = self._voice_client(guild_id)
        if voice_client is None or not voice_client.is_connected():
            raise NotConnectedError("I'm not in a voice channel, use /join first.")
        if self.is_playing(guild_id):
            raise VoiceError("Something is already playing.")

        loop = asyncio.get_running_loop()
        playback: asyncio.Future[None] = loop.create_future()
        playback.add_done_callback(_log_failure)

        def after(error: Exception | None) -> None:
            # Runs on discord.py's sender thread, once the frames run out or on stop().
            with contextlib.suppress(RuntimeError):  # the event loop is already closed
                loop.call_soon_threadsafe(_settle, playback, error)

        try:
            voice_client.play(_OpusFrameSource(frames), after=after)
        except discord.ClientException as exc:
            playback.cancel()
            raise VoiceError(str(exc)) from exc
        self._playbacks[guild_id] = playback
        return playback

    async def stop(self, guild_id: int) -> None:
        voice_client = self._voice_client(guild_id)
        if voice_client is not None:
            voice_client.stop()
        playback = self._playbacks.get(guild_id)
        if playback is not None and not playback.done():
            # Wait for the old sender thread to exit, so that a following play() never has
            # two threads emitting RTP packets (and sequence numbers) at the same time.
            _, pending = await asyncio.wait({playback}, timeout=STOP_TIMEOUT)
            if pending:
                _log.warning("Playback in guild %s did not stop within %ss", guild_id, STOP_TIMEOUT)

    def _voice_client(self, guild_id: int) -> discord.VoiceClient | None:
        guild = self._client.get_guild(guild_id)
        voice_client = guild.voice_client if guild is not None else None
        return voice_client if isinstance(voice_client, discord.VoiceClient) else None

    async def _drop_half_open(self, guild_id: int) -> None:
        voice_client = self._voice_client(guild_id)
        if voice_client is not None and not voice_client.is_connected():
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                _log.debug("Cleanup after a failed connection failed too", exc_info=True)


def _settle(playback: asyncio.Future[None], error: Exception | None) -> None:
    if playback.done():
        return
    if error is None:
        playback.set_result(None)
    else:
        playback.set_exception(error)


def _log_failure(playback: asyncio.Future[None]) -> None:
    # Also marks the exception as retrieved, so asyncio doesn't warn about it at GC time
    # when nobody awaits the playback.
    if not playback.cancelled() and (error := playback.exception()) is not None:
        _log.error("Playback failed", exc_info=error)
