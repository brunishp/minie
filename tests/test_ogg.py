import random
from pathlib import Path

import pytest
from fakes import STEREO_10MS, STEREO_20MS, mux, write_opus

from minie.voice.ogg import OggError, iter_packets, opus_packet_duration_ms, read_opus_frames


@pytest.mark.parametrize("segments_per_page", [1, 2, 3, 255])
def test_packets_survive_lacing_and_page_boundaries(segments_per_page: int) -> None:
    rng = random.Random(0)
    packets = [rng.randbytes(size) for size in (0, 1, 254, 255, 256, 510, 1000, 3000)]
    stream = mux(packets, segments_per_page=segments_per_page)
    assert list(iter_packets(stream)) == packets


def test_rejects_data_that_is_not_ogg() -> None:
    with pytest.raises(OggError, match="no Ogg page"):
        list(iter_packets(b"RIFF" + bytes(64)))


def test_rejects_a_truncated_page() -> None:
    stream = mux([bytes(600)])
    with pytest.raises(OggError, match="truncated page"):
        list(iter_packets(stream[:-1]))


def test_rejects_a_stream_that_stops_inside_a_packet() -> None:
    stream = mux([bytes(600)], segments_per_page=1)
    first_page = 27 + 1 + 255
    with pytest.raises(OggError, match="middle of a packet"):
        list(iter_packets(stream[:first_page]))


def test_rejects_a_second_logical_stream() -> None:
    stream = mux([b"a"], serial=1) + mux([b"b"], serial=2)
    with pytest.raises(OggError, match="not supported"):
        list(iter_packets(stream))


def test_rejects_a_missing_continuation_flag() -> None:
    stream = bytearray(mux([bytes(600)], segments_per_page=1))
    second_page = 27 + 1 + 255
    stream[second_page + 5] = 0x00  # header type byte
    with pytest.raises(OggError, match="continuation"):
        list(iter_packets(bytes(stream)))


def test_reads_20ms_frames(tmp_path: Path) -> None:
    frames = [STEREO_20MS + bytes([i]) * 40 for i in range(5)]
    assert read_opus_frames(write_opus(tmp_path / "ok.ogg", frames)) == frames


def test_rejects_frames_of_another_duration(tmp_path: Path) -> None:
    path = write_opus(tmp_path / "10ms.ogg", [STEREO_20MS, STEREO_10MS])
    with pytest.raises(OggError, match="packet 1 lasts 10 ms"):
        read_opus_frames(path)


def test_rejects_ogg_that_is_not_opus(tmp_path: Path) -> None:
    path = tmp_path / "vorbis.ogg"
    path.write_bytes(mux([b"\x01vorbis" + bytes(23)]))
    with pytest.raises(OggError, match="not an Ogg/Opus file"):
        read_opus_frames(path)


def test_rejects_multichannel_opus(tmp_path: Path) -> None:
    path = write_opus(tmp_path / "surround.ogg", [STEREO_20MS], channels=6, mapping_family=1)
    with pytest.raises(OggError, match="6 channels"):
        read_opus_frames(path)


@pytest.mark.parametrize(
    ("packet", "duration"),
    [
        (STEREO_20MS, 20),
        (STEREO_10MS, 10),
        (bytes([3 << 3]), 60),  # SILK narrowband, 60 ms
        (bytes([13 << 3]), 20),  # hybrid super-wideband, 20 ms
        (bytes([31 << 3 | 1]), 40),  # code 1: two 20 ms frames
        (bytes([28 << 3 | 3, 8]), 20),  # code 3: eight 2.5 ms frames
    ],
)
def test_packet_duration_from_toc(packet: bytes, duration: float) -> None:
    assert opus_packet_duration_ms(packet) == duration
