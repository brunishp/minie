"""FFmpeg helpers. Everything minie plays is encoded once, ahead of time, as Ogg/Opus."""

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from minie.errors import UserError
from minie.voice.base import FRAME_DURATION_MS
from minie.voice.ogg import read_opus_frames

_log = logging.getLogger(__name__)

# Output settings for every file minie writes: 48 kHz stereo Opus in 20 ms frames, which is
# what Discord carries, so playback copies packets without transcoding. 96 kb/s is the
# highest bitrate an unboosted voice channel offers its members. Uploaders' tags and
# chapters are dropped.
# fmt: off
OPUS_OUTPUT_ARGS = (
    "-c:a", "libopus",
    "-b:a", "96k",
    "-ar", "48000",
    "-ac", "2",
    "-frame_duration", "20",
    "-application", "audio",
    "-map_metadata", "-1",
    "-map_chapters", "-1",
    "-f", "ogg",
)
# fmt: on

# The only demuxers allowed to read an upload, by their FFmpeg names (mov covers MP4 and
# M4A, matroska covers WebM). Passed as -format_whitelist, so FFmpeg refuses anything else
# before parsing it: some demuxers, such as HLS and other playlists, open further files or
# URLs named inside their input, and an uploaded file must never steer that.
ACCEPTED_DEMUXERS = ("aac", "aiff", "flac", "matroska", "mov", "mp3", "ogg", "wav")
_INPUT_RESTRICTIONS = (
    *("-format_whitelist", ",".join(ACCEPTED_DEMUXERS)),
    *("-protocol_whitelist", "file"),
)

TOOL_TIMEOUT = 60.0
# EBU R128's absolute gate: a clip measuring below this has no defined loudness, it's silence.
SILENCE_LUFS = -70.0
LOUDNESS_RANGE = 11  # loudnorm's default LRA target


class AudioError(Exception):
    """FFmpeg or ffprobe failed."""


class AudioRejected(UserError):
    """An upload can't become a sound. The message tells the uploader why."""


@dataclass(frozen=True)
class Loudness:
    """What loudnorm's first pass measures, to be fed to its second pass."""

    integrated: float
    true_peak: float
    lra: float
    threshold: float
    offset: float

    def as_loudnorm_options(self) -> str:
        return (
            f"measured_I={self.integrated:.2f}:measured_TP={self.true_peak:.2f}:"
            f"measured_LRA={self.lra:.2f}:measured_thresh={self.threshold:.2f}:"
            f"offset={self.offset:.2f}"
        )


async def convert_upload(
    source: Path,
    dest: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    max_duration: float,
    loudness_lufs: float,
    true_peak_dbtp: float,
) -> int:
    """Turn an uploaded file into a loudness-normalised Ogg/Opus sound written to `dest`.

    Returns the sound's duration in milliseconds. Raises `AudioRejected`, with a message
    for the uploader, when the file can't be used.
    """
    demuxer, claimed_duration = await probe(ffprobe, source)
    if claimed_duration > max_duration:
        raise AudioRejected(
            f"That clip lasts {claimed_duration:.1f} s, the limit is {max_duration:g} s."
        )

    # Force the demuxer ffprobe identified, and decode at most one second past the limit,
    # whatever the container claims: the real length is checked on the encoded result.
    decode = (
        *_INPUT_RESTRICTIONS,
        *("-f", demuxer, "-t", f"{max_duration + 1:g}", "-i", str(source)),
        *("-map", "0:a:0"),  # first audio stream only; any video or cover art is ignored
    )
    loudnorm = f"loudnorm=I={loudness_lufs:g}:TP={true_peak_dbtp:g}:LRA={LOUDNESS_RANGE}"
    partial = dest.with_name(dest.name + ".part")
    try:
        # Two passes: measure, then apply one constant gain (linear=true) computed from
        # the measurement. loudnorm falls back to dynamic compression only when that gain
        # would push peaks above the true peak limit.
        measured = await measure_loudness(ffmpeg, decode, loudnorm)
        if measured.integrated < SILENCE_LUFS:
            raise AudioRejected("That clip is silent.")
        normalise = f"{loudnorm}:{measured.as_loudnorm_options()}:linear=true"
        await _ffmpeg(ffmpeg, *decode, "-af", normalise, *OPUS_OUTPUT_ARGS, str(partial))

        frames = await asyncio.to_thread(read_opus_frames, partial)
        duration_ms = len(frames) * FRAME_DURATION_MS
        # One frame of slack: the encoder's pre-skip lengthens the start by a few ms.
        if duration_ms > max_duration * 1000 + FRAME_DURATION_MS:
            raise AudioRejected(f"That clip is longer than {max_duration:g} s.")
        # Write then rename: a crash mid-encode never leaves a truncated file at `dest`.
        partial.replace(dest)
        return duration_ms
    except AudioError as exc:
        _log.warning("FFmpeg failed on an upload: %s", exc)
        raise AudioRejected("FFmpeg couldn't decode that file.") from exc
    finally:
        partial.unlink(missing_ok=True)


async def probe(ffprobe: str, path: Path) -> tuple[str, float]:
    """Identify an upload: the demuxer to force on FFmpeg, and the duration it claims."""
    code, stdout, _ = await _run(
        ffprobe,
        *("-v", "error", *_INPUT_RESTRICTIONS),
        *("-select_streams", "a", "-show_entries", "format=format_name,duration:stream=index"),
        *("-of", "json", str(path)),
    )
    try:
        info = json.loads(stdout) if code == 0 else {}
    except ValueError:
        info = {}
    container = info.get("format", {})
    # Demuxers can have several names, e.g. "mov,mp4,m4a,3gp,3g2,mj2".
    names = container.get("format_name", "").split(",")
    demuxer = next((name for name in names if name in ACCEPTED_DEMUXERS), None)
    if demuxer is None:
        raise AudioRejected("That file isn't in an audio format I accept.")
    if not info.get("streams"):
        raise AudioRejected("That file contains no audio.")
    try:
        duration = float(container["duration"])
    except KeyError, ValueError:
        raise AudioRejected("I couldn't tell how long that clip is.") from None
    return demuxer, duration


async def measure_loudness(ffmpeg: str, decode: tuple[str, ...], loudnorm: str) -> Loudness:
    """Run loudnorm's measuring pass over the input that `decode` describes."""
    stderr = await _ffmpeg(
        ffmpeg, *decode, "-af", f"{loudnorm}:print_format=json", "-f", "null", "-", quiet=False
    )
    # The measurement is the last JSON object FFmpeg prints.
    start, end = stderr.rfind("{"), stderr.rfind("}")
    try:
        data = json.loads(stderr[start : end + 1])
        return Loudness(
            integrated=float(data["input_i"]),
            true_peak=float(data["input_tp"]),
            lra=float(data["input_lra"]),
            threshold=float(data["input_thresh"]),
            offset=float(data["target_offset"]),
        )
    except (ValueError, KeyError) as exc:
        raise AudioError("loudnorm printed no usable measurement") from exc


async def _ffmpeg(ffmpeg: str, *args: str, quiet: bool = True) -> str:
    # loudnorm prints its measurement at the "info" level.
    level = "error" if quiet else "info"
    code, _, stderr = await _run(
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", level, "-y", *args
    )
    if code != 0:
        raise AudioError(stderr.strip()[-500:] or f"ffmpeg exited with status {code}")
    return stderr


async def _run(*command: str) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"{command[0]} not found") from exc
    try:
        async with asyncio.timeout(TOOL_TIMEOUT):
            stdout, stderr = await process.communicate()
    except TimeoutError as exc:
        raise AudioError(f"{Path(command[0]).name} took longer than {TOOL_TIMEOUT:g} s") from exc
    finally:
        # Also reached when the bot shuts down mid-conversion: never leave FFmpeg running.
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def run_ffmpeg(ffmpeg: str, *args: str) -> None:
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise AudioError(f"{ffmpeg} not found") from exc
    if result.returncode != 0:
        raise AudioError(result.stderr.strip() or f"ffmpeg exited with status {result.returncode}")


def ensure_test_tone(ffmpeg: str, path: Path) -> None:
    """Create the built-in test sound (one second at 880 Hz) unless it already exists."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    run_ffmpeg(
        ffmpeg,
        *("-f", "lavfi", "-i", "sine=frequency=880:duration=1"),
        *("-af", "afade=t=in:d=0.01,afade=t=out:st=0.8:d=0.2"),  # fades avoid clicks
        *OPUS_OUTPUT_ARGS,
        str(partial),
    )
    # Write then rename: a crash mid-encode never leaves a truncated file at `path`.
    partial.replace(path)
