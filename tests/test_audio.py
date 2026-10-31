import shutil
from pathlib import Path

import pytest

from minie.audio import AudioError, ensure_test_tone, run_ffmpeg
from minie.voice.ogg import read_opus_frames

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


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
