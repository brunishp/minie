import shutil
from pathlib import Path

import pytest

from minie import audio
from minie.audio import (
    AudioError,
    AudioRejected,
    convert_upload,
    ensure_test_tone,
    measure_loudness,
    run_ffmpeg,
)
from minie.voice.ogg import read_opus_frames

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def clip(path: Path, source: str, *args: str) -> Path:
    """Generate a test input with one of FFmpeg's lavfi sources."""
    run_ffmpeg("ffmpeg", "-f", "lavfi", "-i", source, *args, str(path))
    return path


def outputs(directory: Path) -> list[Path]:
    """The converted file and its temporary twin, if either was left behind."""
    return list(directory.glob("out.ogg*"))


async def convert(source: Path, dest: Path, *, max_duration: float = 10) -> int:
    return await convert_upload(
        source,
        dest,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        max_duration=max_duration,
        loudness_lufs=-16,
        true_peak_dbtp=-1.5,
    )


async def test_uploads_are_normalised_to_the_target_loudness(tmp_path: Path) -> None:
    quiet = clip(tmp_path / "quiet.wav", "sine=frequency=440:duration=2", "-af", "volume=0.1")
    out = tmp_path / "out.ogg"
    assert 2000 <= await convert(quiet, out) <= 2040
    assert read_opus_frames(out)
    measured = await measure_loudness("ffmpeg", ("-i", str(out)), "loudnorm=I=-16:TP=-1.5")
    assert measured.integrated == pytest.approx(-16, abs=1)


@pytest.mark.parametrize(
    ("name", "codec"),
    [("a.m4a", ("-c:a", "aac")), ("a.mp3", ()), ("a.webm", ("-c:a", "libopus")), ("a.flac", ())],
)
async def test_accepts_common_formats(tmp_path: Path, name: str, codec: tuple[str, ...]) -> None:
    source = clip(tmp_path / name, "sine=frequency=440:duration=1", *codec)
    # As in the library, the upload has no extension: the format comes from its content.
    upload = source.rename(tmp_path / "upload")
    assert await convert(upload, tmp_path / "out.ogg") > 0


async def test_rejects_clips_over_the_limit(tmp_path: Path) -> None:
    long = clip(tmp_path / "long.wav", "sine=frequency=440:duration=12")
    with pytest.raises(AudioRejected, match=r"lasts 12\.0 s"):
        await convert(long, tmp_path / "out.ogg")


async def test_checks_the_real_length_not_the_claimed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lying_probe(ffprobe: str, path: Path) -> tuple[str, float]:
        return "wav", 0.5

    monkeypatch.setattr(audio, "probe", lying_probe)
    three_seconds = clip(tmp_path / "long.wav", "sine=frequency=440:duration=3")
    with pytest.raises(AudioRejected, match="longer than 1 s"):
        await convert(three_seconds, tmp_path / "out.ogg", max_duration=1)
    assert outputs(tmp_path) == []


async def test_rejects_silence(tmp_path: Path) -> None:
    silent = clip(tmp_path / "silent.wav", "anullsrc=r=48000:cl=mono", "-t", "2")
    with pytest.raises(AudioRejected, match="silent"):
        await convert(silent, tmp_path / "out.ogg")
    assert outputs(tmp_path) == []


async def test_rejects_files_that_are_not_audio(tmp_path: Path) -> None:
    upload = tmp_path / "upload"
    upload.write_text("definitely not audio")
    with pytest.raises(AudioRejected, match="format"):
        await convert(upload, tmp_path / "out.ogg")


async def test_refuses_playlists(tmp_path: Path) -> None:
    # An HLS playlist makes FFmpeg open the files it lists: it must never get that far.
    clip(tmp_path / "segment.wav", "sine=frequency=440:duration=1")
    upload = tmp_path / "upload"
    upload.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nsegment.wav\n#EXT-X-ENDLIST\n")
    with pytest.raises(AudioRejected, match="format"):
        await convert(upload, tmp_path / "out.ogg")


def test_test_tone_is_one_second_of_20ms_frames(tmp_path: Path) -> None:
    path = tmp_path / "builtin" / "tone.ogg"
    ensure_test_tone("ffmpeg", path)
    frames = read_opus_frames(path)  # also checks that every packet lasts 20 ms
    # One second plus the encoder's pre-skip (312 samples), padded to whole frames.
    assert 50 <= len(frames) <= 52
    assert not path.with_name(path.name + ".part").exists()


def test_test_tone_is_created_only_once(tmp_path: Path) -> None:
    path = tmp_path / "tone.ogg"
    ensure_test_tone("ffmpeg", path)
    created = path.stat().st_mtime_ns
    ensure_test_tone("ffmpeg", path)
    assert path.stat().st_mtime_ns == created


def test_ffmpeg_failures_carry_its_message(tmp_path: Path) -> None:
    with pytest.raises(AudioError, match=r"missing\.wav"):
        run_ffmpeg("ffmpeg", "-i", str(tmp_path / "missing.wav"), str(tmp_path / "out.ogg"))
