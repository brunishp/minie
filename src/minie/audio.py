"""FFmpeg helpers. Everything minie plays is encoded once, ahead of time, as Ogg/Opus."""

import subprocess
from pathlib import Path

# Output settings for every file minie writes: 48 kHz stereo Opus in 20 ms frames, which is
# what Discord carries, so playback copies packets without transcoding. 96 kb/s is the
# highest bitrate an unboosted voice channel offers its members.
# fmt: off
OPUS_OUTPUT_ARGS = (
    "-c:a", "libopus",
    "-b:a", "96k",
    "-ar", "48000",
    "-ac", "2",
    "-frame_duration", "20",
    "-application", "audio",
    "-map_metadata", "-1",
    "-f", "ogg",
)
# fmt: on


class AudioError(Exception):
    pass


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
