"""Text to speech for /say: Piper's own program, kept running only while it's in use.

Piper runs as a separate program, like FFmpeg. A crash or a stuck run can't take the voice
connections with it, it never blocks the event loop, and its GPL code stays apart from the bot.
Loading its model takes far longer than speaking a sentence, so it stays running for a while
after each use. Then it exits, which gives all of its memory back to the system.
"""

import asyncio
import logging
import re
from pathlib import Path

from minie import audio
from minie.errors import UserError
from minie.tools import tool_environment
from minie.voice.ogg import read_opus_frames

_log = logging.getLogger(__name__)

MAX_TEXT = 200
SYNTHESIS_TIMEOUT = 30.0
STOP_TIMEOUT = 2.0
# Mentions and custom emoji: read out, Discord's markup for them is a long string of digits.
_DISCORD_MARKUP = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d+>")
# Piper's program writes one WAV per input line, and this log line is its only signal that a
# file is complete.
_WROTE = re.compile(r"Wrote (.+/\d+\.wav)\s*$")


class SpeechError(UserError):
    pass


def speakable(text: str) -> str:
    """The single line to give Piper for `text`, or `UserError` if there's nothing to say."""
    line = " ".join(_DISCORD_MARKUP.sub(" ", text).split())
    line = "".join(char for char in line if char.isprintable())
    if len(line) > MAX_TEXT:
        raise UserError(f"That's too long: {MAX_TEXT} characters at most.")
    # Punctuation alone gives no audio, and Piper's program exits with an error on that.
    if not any(char.isalnum() for char in line):
        raise UserError("There's nothing to say in that.")
    return line


def piper_environment() -> dict[str, str]:
    # Text goes to Piper as UTF-8, whatever locale it would otherwise assume.
    return {**tool_environment(), "PYTHONUTF8": "1"}


def written_file(log_line: str, work_dir: Path) -> Path | None:
    """The file a line of Piper's log announces, if any.

    The caller reads and then deletes that file, so only one in `work_dir` counts, whatever
    else ends up in the log.
    """
    match = _WROTE.search(log_line)
    if match is None:
        return None
    path = Path(match[1])
    return path if path.parent == work_dir else None


class PiperWorker:
    """Piper's program, started on demand and stopped after `keep` seconds without use.

    `keep` at 0 stops it after every sentence. If it hangs or dies, it's killed, and the next
    sentence starts a new one.
    """

    def __init__(self, command: str, voice: Path, work_dir: Path, *, keep: float) -> None:
        self._command = command
        self._voice = voice
        self._work_dir = work_dir.resolve()
        self._keep = keep
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._idle_stop: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def synthesise(self, line: str) -> Path:
        """Returns the WAV file for `line`. Deleting it is up to the caller, before the next
        sentence: a new process clears the directory."""
        async with self._lock:
            try:
                async with asyncio.timeout(SYNTHESIS_TIMEOUT):
                    return await self._exchange(line)
            except BaseException as error:
                # An unfinished sentence would be taken for the next one's answer.
                await self._kill()
                if isinstance(error, OSError | TimeoutError):
                    _log.warning("Piper failed: %r", error)
                    raise SpeechError("I couldn't say that, try again.") from error
                raise
            finally:
                await self._schedule_stop()

    async def close(self) -> None:
        if self._idle_stop is not None:
            self._idle_stop.cancel()
        if self._lock.locked():  # mid-sentence: nobody else may read its output while it stops
            await self._kill()
            return
        async with self._lock:
            await self._stop()

    async def _exchange(self, line: str) -> Path:
        process = await self._started()
        assert process.stdin is not None and process.stderr is not None
        process.stdin.write(f"{line}\n".encode())
        await process.stdin.drain()
        while answer := await process.stderr.readline():
            decoded = answer.decode(errors="replace")
            if (path := written_file(decoded, self._work_dir)) is not None:
                return path
            _log.debug("Piper: %s", decoded.rstrip())
        raise OSError(f"Piper exited with status {await process.wait()}")

    async def _started(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        await asyncio.to_thread(self._prepare_work_dir)
        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *("-m", str(self._voice), "-d", str(self._work_dir)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=piper_environment(),
        )
        _log.debug("Started Piper, process %s", self._process.pid)
        return self._process

    def _prepare_work_dir(self) -> None:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        # Whatever is still here, a process that crashed or was killed left half written.
        for leftover in self._work_dir.glob("*.wav"):
            leftover.unlink(missing_ok=True)

    async def _schedule_stop(self) -> None:
        if self._idle_stop is not None:
            self._idle_stop.cancel()
        if self._keep <= 0:
            await self._stop()
        elif self.running:
            self._idle_stop = asyncio.create_task(self._stop_when_idle())

    async def _stop_when_idle(self) -> None:
        await asyncio.sleep(self._keep)
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await process.communicate()  # closes its input, which ends its read loop
        except TimeoutError:
            process.kill()
            await process.wait()
        _log.debug("Stopped Piper")

    async def _kill(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


class Speech:
    """Text in, 20 ms Opus frames out, like any other sound."""

    def __init__(
        self,
        worker: PiperWorker,
        *,
        ffmpeg: str,
        pitch: float,
        loudness_lufs: float,
        true_peak_dbtp: float,
    ) -> None:
        self._worker = worker
        self._ffmpeg = ffmpeg
        self._pitch = pitch
        self._loudness_lufs = loudness_lufs
        self._true_peak_dbtp = true_peak_dbtp
        # One sentence at a time, for the CPU's sake: members already wait out a cooldown.
        self._turn = asyncio.Lock()

    async def frames(self, text: str) -> list[bytes]:
        line = speakable(text)
        async with self._turn:
            wav = await self._worker.synthesise(line)
            opus = wav.with_suffix(".ogg")
            try:
                await audio.encode_speech(
                    wav,
                    opus,
                    ffmpeg=self._ffmpeg,
                    pitch=self._pitch,
                    loudness_lufs=self._loudness_lufs,
                    true_peak_dbtp=self._true_peak_dbtp,
                )
                return await asyncio.to_thread(read_opus_frames, opus)
            except audio.AudioError as error:
                _log.warning("Could not encode speech: %s", error)
                raise SpeechError("I couldn't say that, try again.") from error
            finally:
                await asyncio.to_thread(_delete, wav, opus)

    async def close(self) -> None:
        await self._worker.close()


def _delete(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
