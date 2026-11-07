"""Startup check: refuse to run unless the whole voice stack is usable.

Since 2026-03-01 Discord closes voice connections that don't speak DAVE (close code
4017). A missing piece here would otherwise only show up later, as a failed /join.
"""

import ctypes.util
import importlib
import importlib.metadata
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import discord
import discord.opus

from minie import speech
from minie.config import Settings
from minie.tools import tool_environment


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    """The version found when `ok`, otherwise what is wrong and how to fix it."""


def run_checks(settings: Settings) -> list[Check]:
    return [
        Check("Python", True, platform.python_version()),
        Check("discord.py", True, discord.__version__),
        _check_pynacl(),
        _check_davey(),
        _check_opus(settings.opus_library),
        _check_ffmpeg(settings.ffmpeg),
        _check_ffprobe(settings.ffprobe),
        _check_web(settings),
        _check_piper(settings),
    ]


def _check_pynacl() -> Check:
    try:
        importlib.import_module("nacl.secret")
    except ImportError as exc:
        return Check("PyNaCl", False, f"cannot be imported ({exc}); run `uv sync`")
    return Check("PyNaCl", True, importlib.metadata.version("PyNaCl"))


def _check_davey() -> Check:
    try:
        davey = importlib.import_module("davey")
    except ImportError as exc:
        return Check("davey (DAVE)", False, f"cannot be imported ({exc}); run `uv sync`")
    return Check(
        "davey (DAVE)", True, f"{davey.__version__}, DAVE protocol v{davey.DAVE_PROTOCOL_VERSION}"
    )


def _check_opus(library: str | None) -> Check:
    if not discord.opus.is_loaded():
        name = library or ctypes.util.find_library("opus")
        if name is None:
            return Check(
                "libopus",
                False,
                "not found; install it (`brew install opus`, `apt install libopus0`) "
                "or point MINIE_OPUS_LIBRARY at the shared library",
            )
        try:
            discord.opus.load_opus(name)
        except OSError as exc:
            return Check("libopus", False, f"{name} could not be loaded: {exc}")
    return Check("libopus", True, discord.opus.Encoder.get_opus_version())


def _check_ffmpeg(command: str) -> Check:
    path = shutil.which(command)
    if path is None:
        return Check(
            "ffmpeg",
            False,
            f"`{command}` not found; install FFmpeg (`brew install ffmpeg`, `apt install ffmpeg`) "
            "or set MINIE_FFMPEG",
        )
    encoders = _output(path, "-hide_banner", "-encoders")
    if " libopus " not in encoders:
        return Check("ffmpeg", False, f"{path} was built without the libopus encoder")
    return Check("ffmpeg", True, f"{_version(path)} ({path})")


def _check_ffprobe(command: str) -> Check:
    path = shutil.which(command)
    if path is None:
        return Check(
            "ffprobe", False, f"`{command}` not found; it ships with FFmpeg, or set MINIE_FFPROBE"
        )
    return Check("ffprobe", True, f"{_version(path)} ({path})")


def _check_web(settings: Settings) -> Check:
    if settings.web_port is None:
        return Check("web UI", True, "off (MINIE_WEB_PORT is not set)")
    if settings.oauth_client_secret is None:
        return Check(
            "web UI",
            False,
            "MINIE_OAUTH_CLIENT_SECRET is not set (Developer Portal > OAuth2 > Client Secret)",
        )
    return Check(
        "web UI", True, f"{settings.web_url}, listening on {settings.web_host}:{settings.web_port}"
    )


def _check_piper(settings: Settings) -> Check:
    name = "Piper (/say)"
    voice = settings.tts_voice
    if voice is None:
        return Check(name, True, "off (MINIE_TTS_VOICE is not set)")
    if not voice.exists():
        return Check(name, False, f"voice file {voice} not found; see the README")
    path = shutil.which(settings.piper)
    if path is None:
        return Check(name, False, f"{settings.piper} not found; see the README")
    # A real sentence: the bot relies on the exact log line Piper's program writes, which an
    # update could change.
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp).resolve()
        try:
            result = subprocess.run(
                [path, "-m", str(voice), "-d", str(work_dir)],
                input="Bonjour.\n",
                # As the bot runs it. Its output isn't read, and a pipe can make it hang.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
                env=speech.piper_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check(name, False, f"could not run it: {exc}")
        lines = result.stderr.splitlines()
        written = (speech.written_file(line, work_dir) for line in lines)
        if not any(file is not None and file.exists() for file in written):
            said = lines[-1] if lines else "nothing"
            return Check(name, False, f"it announced no audio file; it said: {said}")
    return Check(name, True, f"{voice.name} ({path})")


def _output(*command: str) -> str:
    # With the environment the bot runs these programs in, so the check reflects real use.
    return subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False, env=tool_environment()
    ).stdout


def _version(path: str) -> str:
    # First line looks like "ffmpeg version 7.1.1-1+b1 Copyright (c) 2000-2025 ...".
    words = _output(path, "-hide_banner", "-version").split()
    return words[2] if len(words) > 2 and words[1] == "version" else "unknown version"
