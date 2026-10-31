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
from dataclasses import dataclass

import discord
import discord.opus

from minie.config import Settings


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


def _output(*command: str) -> str:
    return subprocess.run(command, capture_output=True, text=True, timeout=10, check=False).stdout


def _version(path: str) -> str:
    # First line looks like "ffmpeg version 7.1.1-1+b1 Copyright (c) 2000-2025 ...".
    words = _output(path, "-hide_banner", "-version").split()
    return words[2] if len(words) > 2 and words[1] == "version" else "unknown version"
