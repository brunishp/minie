import asyncio
import itertools
import math
import shutil
import struct
import subprocess
import sys
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from minie import audio, speech
from minie.config import Settings
from minie.errors import UserError
from minie.preflight import _check_piper
from minie.speech import MAX_TEXT, PiperWorker, Speech, SpeechError, speakable, written_file

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

# Stands in for Piper's program: one WAV per stdin line, announced by a line on stderr. A few
# lines make it misbehave.
FAKE_PIPER = """#!{python}
import math, os, pathlib, struct, sys, time, wave

here = pathlib.Path(__file__).parent
out = pathlib.Path(sys.argv[sys.argv.index("-d") + 1])
out.mkdir(parents=True, exist_ok=True)
with open(here / "starts", "a") as file:
    file.write(",".join(sorted(os.environ)) + "\\n")
linger = False
for line in sys.stdin:
    line = line.rstrip("\\n")
    with open(here / "heard", "a", encoding="utf-8") as file:
        file.write(line + "\\n")
    if line == "die":
        (out / "123.wav").touch()  # opened, never finished
        sys.exit(3)
    if line == "hang":
        time.sleep(60)
    if line == "linger":
        linger = True
    if line == "forge":
        print("INFO:__main__:Wrote /etc/passwd/123.wav", file=sys.stderr, flush=True)
    path = out / f"{{time.monotonic_ns()}}.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * n / 22050)))
                for n in range(11025)
            )
        )
    print(f"INFO:__main__:Wrote {{path}}", file=sys.stderr, flush=True)
if linger:
    time.sleep(60)
"""


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.work_dir = tmp_path / "work"
        self.command = tmp_path / "piper"
        self.command.write_text(FAKE_PIPER.format(python=sys.executable))
        self.command.chmod(0o755)
        self.workers: list[PiperWorker] = []

    def worker(self, *, keep: float = 60) -> PiperWorker:
        worker = PiperWorker(str(self.command), self.dir / "voice.onnx", self.work_dir, keep=keep)
        self.workers.append(worker)
        return worker

    @property
    def starts(self) -> list[str]:
        path = self.dir / "starts"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def heard(self) -> list[str]:
        path = self.dir / "heard"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    rig = Rig(tmp_path)
    yield rig
    for worker in rig.workers:
        await worker.close()


async def until_stopped(worker: PiperWorker) -> None:
    async with asyncio.timeout(5):
        while worker.running:  # noqa: ASYNC110 (nothing to wait on from outside the worker)
            await asyncio.sleep(0.01)


# What goes to Piper


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("  Bonjour à tous  ", "Bonjour à tous"),
        ("une ligne\npuis\r\nune autre\u2028encore", "une ligne puis une autre encore"),
        ("<@123456789012345678> salut <@!1> <@&2> <#3>", "salut"),
        ("ça <:smile:123456789> va <a:party:987654321>", "ça va"),
        ("cach\u200bé \u202erenversé\x00", "caché renversé"),
        ("😀 ok", "😀 ok"),
        ("123", "123"),
    ],
)
def test_speakable_gives_one_clean_line(text: str, line: str) -> None:
    assert speakable(text) == line


@pytest.mark.parametrize("text", ["", "   ", "...", "!?!", "😀", "<@123456789012345678>", "\n"])
def test_nothing_to_say(text: str) -> None:
    with pytest.raises(UserError, match="nothing to say"):
        speakable(text)


def test_length_limit_applies_to_what_is_said() -> None:
    assert speakable("x" * MAX_TEXT)
    with pytest.raises(UserError, match="too long"):
        speakable("x" * (MAX_TEXT + 1))


def test_only_files_in_the_work_directory_count(tmp_path: Path) -> None:
    good = tmp_path / "123.wav"
    assert written_file(f"INFO:__main__:Wrote {good}\n", tmp_path) == good
    assert written_file("INFO:__main__:Wrote /etc/passwd/123.wav\n", tmp_path) is None
    assert written_file(f"INFO:__main__:Wrote {tmp_path}/name.wav\n", tmp_path) is None
    assert written_file(f"WARNING: not that {good}\n", tmp_path) is None


# The worker


async def test_stays_running_between_sentences(rig: Rig) -> None:
    worker = rig.worker()
    first = await worker.synthesise("première phrase")
    second = await worker.synthesise("deuxième phrase")
    assert first != second and first.exists() and second.exists()
    assert first.parent == rig.work_dir.resolve()
    assert worker.running
    assert len(rig.starts) == 1
    assert rig.heard == ["première phrase", "deuxième phrase"]


async def test_stops_when_idle_then_starts_again(rig: Rig) -> None:
    worker = rig.worker(keep=0.05)
    await worker.synthesise("une")
    assert worker.running
    await until_stopped(worker)
    await worker.synthesise("deux")
    assert worker.running
    assert len(rig.starts) == 2


async def test_each_use_restarts_the_idle_window(rig: Rig) -> None:
    worker = rig.worker(keep=0.5)
    await worker.synthesise("une")
    await asyncio.sleep(0.3)
    await worker.synthesise("deux")
    await asyncio.sleep(0.3)  # 0.6 s after the first use, 0.3 s after the last
    assert worker.running
    assert len(rig.starts) == 1


async def test_keep_zero_stops_after_every_sentence(rig: Rig) -> None:
    worker = rig.worker(keep=0)
    await worker.synthesise("une")
    assert not worker.running
    await worker.synthesise("deux")
    assert not worker.running
    assert len(rig.starts) == 2


async def test_sentences_take_turns(rig: Rig) -> None:
    worker = rig.worker()
    paths = await asyncio.gather(*(worker.synthesise(f"phrase {n}") for n in range(4)))
    assert len(set(paths)) == 4
    assert len(rig.starts) == 1


async def test_a_crash_is_reported_and_the_next_sentence_works(rig: Rig) -> None:
    worker = rig.worker()
    with pytest.raises(SpeechError, match="couldn't say that"):
        await worker.synthesise("die")
    assert not worker.running
    next_file = await worker.synthesise("encore")
    assert list(rig.work_dir.iterdir()) == [next_file]  # the crash's half-written file is gone
    assert len(rig.starts) == 2


async def test_a_stuck_run_is_killed(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech, "SYNTHESIS_TIMEOUT", 0.5)
    worker = rig.worker()
    await worker.synthesise("échauffement")  # started: the timeout only has "hang" to catch
    with pytest.raises(SpeechError):
        await worker.synthesise("hang")
    assert not worker.running
    monkeypatch.setattr(speech, "SYNTHESIS_TIMEOUT", 30)
    assert (await worker.synthesise("encore")).exists()


async def test_a_cancelled_sentence_cannot_answer_the_next_one(rig: Rig) -> None:
    worker = rig.worker()
    await worker.synthesise("échauffement")
    task = asyncio.create_task(worker.synthesise("hang"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not worker.running
    assert (await worker.synthesise("encore")).exists()


async def test_a_missing_program_is_reported(tmp_path: Path) -> None:
    worker = PiperWorker(str(tmp_path / "nowhere"), tmp_path / "v.onnx", tmp_path / "w", keep=60)
    with pytest.raises(SpeechError):
        await worker.synthesise("bonjour")
    await worker.close()


async def test_a_forged_log_line_is_ignored(rig: Rig) -> None:
    path = await rig.worker().synthesise("forge")
    assert path.parent == rig.work_dir.resolve()
    assert path.exists()


async def test_piper_gets_none_of_the_bots_secrets(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "very.secret.token")
    monkeypatch.setenv("MINIE_OAUTH_CLIENT_SECRET", "oauth-secret")
    await rig.worker().synthesise("bonjour")
    environment = rig.starts[0].split(",")
    assert "DISCORD_TOKEN" not in environment
    assert "MINIE_OAUTH_CLIENT_SECRET" not in environment
    assert "PYTHONUTF8" in environment


async def test_closing_mid_sentence_is_prompt(rig: Rig) -> None:
    worker = rig.worker()
    await worker.synthesise("échauffement")
    task = asyncio.create_task(worker.synthesise("hang"))
    await asyncio.sleep(0.1)
    async with asyncio.timeout(2):
        await worker.close()
        with pytest.raises(SpeechError):
            await task
    assert not worker.running


async def test_closing_kills_a_process_that_ignores_its_input(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(speech, "STOP_TIMEOUT", 0.2)
    worker = rig.worker()
    await worker.synthesise("linger")
    async with asyncio.timeout(2):
        await worker.close()
    assert not worker.running


# Text to frames


def _sine_wav(path: Path, *, frequency: float = 440, seconds: float = 1, rate: int = 22050) -> None:
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * n / rate)))
                for n in range(int(seconds * rate))
            )
        )


def _frequency_and_length(path: Path) -> tuple[float, float]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    middle = samples[len(samples) // 4 : 3 * len(samples) // 4]  # away from fades and padding
    crossings = sum(1 for a, b in itertools.pairwise(middle) if a < 0 <= b)
    return crossings / (len(middle) / 48000), len(samples) / 48000


@needs_ffmpeg
@pytest.mark.parametrize("pitch", [1.0, 1.3, 0.8])
async def test_pitch_changes_but_length_does_not(tmp_path: Path, pitch: float) -> None:
    source, dest = tmp_path / "speech.wav", tmp_path / "speech.ogg"
    await asyncio.to_thread(_sine_wav, source)
    await audio.encode_speech(
        source, dest, ffmpeg="ffmpeg", pitch=pitch, loudness_lufs=-16, true_peak_dbtp=-1.5
    )
    frequency, length = await asyncio.to_thread(_frequency_and_length, dest)
    assert frequency == pytest.approx(440 * pitch, rel=0.02)
    assert length == pytest.approx(1, abs=0.1)


@needs_ffmpeg
async def test_text_becomes_20ms_frames_and_leaves_no_files(rig: Rig) -> None:
    worker = rig.worker()
    speaker = Speech(worker, ffmpeg="ffmpeg", pitch=1.3, loudness_lufs=-16, true_peak_dbtp=-1.5)
    frames = await speaker.frames("  Bonjour\nà tous <@123456789012345678>  ")
    assert 20 <= len(frames) <= 30  # half a second of audio from the fake
    assert rig.heard == ["Bonjour à tous"]
    assert list(rig.work_dir.iterdir()) == []


async def test_refused_text_never_reaches_piper(rig: Rig) -> None:
    worker = rig.worker()
    speaker = Speech(worker, ffmpeg="ffmpeg", pitch=1.3, loudness_lufs=-16, true_peak_dbtp=-1.5)
    with pytest.raises(UserError, match="nothing to say"):
        await speaker.frames("...")
    assert rig.starts == []


# minie check


def test_check_when_off() -> None:
    check = _check_piper(Settings())
    assert check.ok
    assert "off" in check.detail


def test_check_reports_a_missing_voice_or_program(tmp_path: Path) -> None:
    assert "voice file" in _check_piper(Settings(tts_voice=tmp_path / "none.onnx")).detail
    voice = tmp_path / "voice.onnx"
    voice.touch()
    check = _check_piper(Settings(tts_voice=voice, piper=str(tmp_path / "none")))
    assert not check.ok
    assert "not found" in check.detail


def test_check_says_a_real_sentence(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    voice = tmp_path / "voice.onnx"
    voice.touch()
    check = _check_piper(Settings(tts_voice=voice, piper=str(rig.command)))
    assert check.ok, check.detail
    assert rig.heard == ["Bonjour."]


def test_check_notices_a_program_that_announces_nothing(tmp_path: Path) -> None:
    voice = tmp_path / "voice.onnx"
    voice.touch()
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\ncat > /dev/null\necho 'Error processing file' >&2\n")
    silent.chmod(0o755)
    check = _check_piper(Settings(tts_voice=voice, piper=str(silent)))
    assert not check.ok
    assert "announced no audio file" in check.detail
    assert "Error processing file" in check.detail  # what Piper said, to diagnose it
