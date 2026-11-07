from pathlib import Path

import pytest

from minie import audio, preflight
from minie.tools import tool_environment

SECRETS = ("DISCORD_TOKEN", "MINIE_OAUTH_CLIENT_SECRET")
OTHER = "SOME_OTHER_VARIABLE"


@pytest.fixture
def seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for FFmpeg that records its environment, with secrets in the bot's own."""
    for name in (*SECRETS, OTHER):
        monkeypatch.setenv(name, "very-secret-value")
    tool = tmp_path / "tool"
    tool.write_text(f'#!/bin/sh\nenv > "{tmp_path}/seen"\n')
    tool.chmod(0o755)
    return tmp_path / "seen"


def assert_no_secrets(seen: Path) -> None:
    recorded = seen.read_text()
    assert "very-secret-value" not in recorded
    assert "PATH=" in recorded


async def test_the_upload_pipeline_gets_no_secrets(seen: Path) -> None:
    await audio._ffmpeg(str(seen.with_name("tool")), "-version")
    assert_no_secrets(seen)


def test_the_test_tone_gets_no_secrets(seen: Path) -> None:
    audio.run_ffmpeg(str(seen.with_name("tool")), "-version")
    assert_no_secrets(seen)


def test_the_startup_check_gets_no_secrets(seen: Path) -> None:
    preflight._output(str(seen.with_name("tool")), "-version")
    assert_no_secrets(seen)


def test_only_what_a_program_needs_to_run_is_passed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/minie")
    monkeypatch.setenv("TMPDIR", "/tmp/minie")
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    monkeypatch.setenv(OTHER, "secret")
    assert tool_environment() == {
        "PATH": "/usr/bin",
        "HOME": "/home/minie",
        "TMPDIR": "/tmp/minie",
    }
    monkeypatch.delenv("TMPDIR")
    assert "TMPDIR" not in tool_environment()
