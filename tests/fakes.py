"""Stand-ins shared by the tests: the voice layer, Discord presence, a clock, Ogg/Opus files."""

import asyncio
import struct
from pathlib import Path

from minie.control import Caller, NotAMember
from minie.errors import UserError
from minie.voice.base import NotConnectedError, OpusFrames, VoiceError

STEREO_20MS = bytes([31 << 3 | 0b100])  # TOC: CELT fullband 20 ms, stereo, one frame
STEREO_10MS = bytes([30 << 3 | 0b100])  # TOC: CELT fullband 10 ms, stereo, one frame
OPUS_TAGS = b"OpusTags" + struct.pack("<I", 4) + b"test" + struct.pack("<I", 0)


class FakeVoice:
    """A VoicePlayer whose sounds play until the test calls `finish`."""

    def __init__(self, channel: int | None = None) -> None:
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


class FakePresence:
    """Discord as the rules see it: members by ID, channels the bot can't join or that
    members can't see."""

    def __init__(self) -> None:
        self.members: dict[tuple[int, int], Caller] = {}
        self.unjoinable: dict[int, str] = {}
        """Channel ID → the reason the bot can't join it."""
        self.names: dict[int, str] = {}
        self.hidden_from: set[tuple[int, int]] = set()
        """(user ID, channel ID) pairs where the member can't view the channel."""
        self.restricted: set[str] = set()
        """Slash commands restricted in Server Settings > Integrations."""

    def add(self, caller: Caller) -> Caller:
        self.members[(caller.guild_id, caller.user_id)] = caller
        return caller

    def check_can_join(self, guild_id: int, channel_id: int) -> None:
        if channel_id in self.unjoinable:
            raise UserError(self.unjoinable[channel_id])

    async def caller(self, guild_id: int, user_id: int) -> Caller:
        try:
            return self.members[(guild_id, user_id)]
        except KeyError:
            raise NotAMember("You're not a member of that server, or I'm not in it.") from None

    async def visible_channel_name(self, caller: Caller, channel_id: int) -> str | None:
        if (caller.user_id, channel_id) in self.hidden_from:
            return None
        return self.names.get(channel_id)

    async def restricted_commands(self, guild_id: int) -> frozenset[str]:
        return frozenset(self.restricted)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def mux(packets: list[bytes], *, segments_per_page: int = 255, serial: int = 1) -> bytes:
    """A tiny Ogg muxer: lace the packets into segments, then cut the segments into pages."""
    segments: list[bytes] = []
    for packet in packets:
        full, _ = divmod(len(packet), 255)
        segments += [packet[i * 255 : (i + 1) * 255] for i in range(full)]
        segments.append(packet[full * 255 :])
    out = bytearray()
    continued = False
    for sequence, start in enumerate(range(0, len(segments), segments_per_page)):
        page = segments[start : start + segments_per_page]
        flags = 0x01 if continued else 0x00
        out += struct.pack("<4sBBqIIIB", b"OggS", 0, flags, 0, serial, sequence, 0, len(page))
        out += bytes(len(segment) for segment in page) + b"".join(page)
        continued = len(page[-1]) == 255
    return bytes(out)


def opus_head(*, channels: int = 2, mapping_family: int = 0) -> bytes:
    return b"OpusHead" + struct.pack("<BBHIhB", 1, channels, 312, 48000, 0, mapping_family)


def write_opus(path: Path, frames: list[bytes], **head: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mux([opus_head(**head), OPUS_TAGS, *frames]))
    return path
