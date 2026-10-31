"""Minimal Ogg/Opus reader: Ogg pages (RFC 3533) and the Opus mapping (RFC 7845).

Kept independent of discord.py so that any `VoicePlayer`, including a hand-written one,
can use it. It only needs to handle what minie writes itself: a single logical stream,
mono or stereo, every packet exactly 20 ms long.
"""

import struct
from collections.abc import Iterator
from pathlib import Path

from minie.voice.base import FRAME_DURATION_MS

# Capture pattern, version, header type, granule position, stream serial number,
# page sequence number, CRC, number of lacing values.
_PAGE_HEADER = struct.Struct("<4sBBqIIIB")
_CONTINUED_PACKET = 0x01

# Frame duration in ms for each of the 32 TOC configurations (RFC 6716, section 3.1):
# SILK-only 0-11, hybrid 12-15, CELT-only 16-31.
_FRAME_MS: tuple[float, ...] = (10, 20, 40, 60) * 3 + (10, 20) * 2 + (2.5, 5, 10, 20) * 4


class OggError(ValueError):
    pass


def iter_packets(data: bytes) -> Iterator[bytes]:
    """Yield the packets of a single-stream Ogg file, reassembled across pages.

    Page CRCs are not checked: the files come from minie's own FFmpeg pipeline.
    """
    pos = 0
    serial: int | None = None
    packet = bytearray()
    while pos < len(data):
        if len(data) - pos < _PAGE_HEADER.size:
            raise OggError(f"truncated page header at byte {pos}")
        capture, version, header_type, _, page_serial, _, _, segments = _PAGE_HEADER.unpack_from(
            data, pos
        )
        if capture != b"OggS" or version != 0:
            raise OggError(f"no Ogg page at byte {pos}")
        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise OggError("multiplexed or chained Ogg streams are not supported")
        if bool(header_type & _CONTINUED_PACKET) != bool(packet):
            raise OggError(f"inconsistent packet continuation at byte {pos}")

        pos += _PAGE_HEADER.size
        lacing = data[pos : pos + segments]
        pos += segments
        if len(lacing) != segments or pos + sum(lacing) > len(data):
            raise OggError("truncated page")
        for size in lacing:
            packet += data[pos : pos + size]
            pos += size
            # A lacing value of 255 means the packet continues in the next segment,
            # possibly on the next page; anything smaller ends it.
            if size < 255:
                yield bytes(packet)
                packet.clear()
    if packet:
        raise OggError("file ends in the middle of a packet")


def read_opus_frames(path: Path) -> list[bytes]:
    """Load an Ogg/Opus file as a list of 20 ms Opus packets.

    Loading everything up front means a bad file fails here, before playback starts,
    and the 20 ms sender thread never waits on the disk.

    Only the packets are sent to Discord, so instructions that the OpusHead header gives
    a local decoder (pre-skip, output gain) are lost. That's harmless for files minie
    encodes itself: the pre-skip is a few milliseconds of encoder delay and the gain is 0.
    """
    packets = iter_packets(path.read_bytes())
    head = next(packets, b"")
    if not head.startswith(b"OpusHead") or len(head) < 19:
        raise OggError(f"{path.name} is not an Ogg/Opus file")
    channels, mapping_family = head[9], head[18]
    if mapping_family != 0 or channels not in (1, 2):
        raise OggError(f"{path.name} has {channels} channels, only mono and stereo are supported")
    if not next(packets, b"").startswith(b"OpusTags"):
        raise OggError(f"{path.name} lacks the OpusTags header")

    frames = list(packets)
    for index, frame in enumerate(frames):
        duration = opus_packet_duration_ms(frame)
        if duration != FRAME_DURATION_MS:
            raise OggError(
                f"{path.name}: packet {index} lasts {duration:g} ms, "
                f"expected {FRAME_DURATION_MS} ms"
            )
    return frames


def opus_packet_duration_ms(packet: bytes) -> float:
    """Audio duration of one Opus packet, read from its TOC byte (RFC 6716, section 3.1)."""
    if not packet:
        raise OggError("empty Opus packet")
    toc = packet[0]
    frame_ms = _FRAME_MS[toc >> 3]
    match toc & 0b11:
        case 0:
            frames = 1
        case 1 | 2:
            frames = 2
        case _:
            if len(packet) < 2:
                raise OggError("truncated Opus packet")
            frames = packet[1] & 0b0011_1111
    return frame_ms * frames
