from pathlib import Path

import pytest

from minie.config import ConfigError, Settings
from minie.playback import Overlap


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.token is None
    assert settings.dev_guild_id is None
    assert settings.data_dir == Path("data")
    assert settings.log_level == "INFO"
    assert settings.voice_debug is False


def test_reads_the_environment() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": " abc.def.ghi \n",
            "MINIE_DEV_GUILD_ID": "123456789012345678",
            "MINIE_DATA_DIR": "/var/lib/minie",
            "MINIE_LOG_LEVEL": "debug",
            "MINIE_VOICE_DEBUG": "yes",
        }
    )
    assert settings.require_token() == "abc.def.ghi"
    assert settings.dev_guild_id == 123456789012345678
    assert settings.data_dir == Path("/var/lib/minie")
    assert settings.log_level == "DEBUG"
    assert settings.voice_debug is True


def test_sound_limits_and_playback_rules() -> None:
    defaults = Settings.from_env({})
    assert defaults.max_upload_bytes == 8 * 2**20
    assert defaults.max_duration == 10
    assert defaults.overlap is Overlap.INTERRUPT
    assert defaults.cooldown == 3

    settings = Settings.from_env(
        {
            "MINIE_MAX_UPLOAD_MB": "2.5",
            "MINIE_MAX_DURATION_S": "4",
            "MINIE_LOUDNESS_LUFS": "-23",
            "MINIE_MAX_SOUNDS": "20",
            "MINIE_COOLDOWN_S": "0",
            "MINIE_OVERLAP": "Queue",
            "MINIE_QUEUE_MAX": "3",
        }
    )
    assert settings.max_upload_bytes == round(2.5 * 2**20)
    assert settings.max_duration == 4
    assert settings.loudness_lufs == -23
    assert settings.max_sounds == 20
    assert settings.cooldown == 0
    assert settings.overlap is Overlap.QUEUE
    assert settings.queue_max == 3


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MINIE_DEV_GUILD_ID", "my-server"),
        ("MINIE_DEV_GUILD_ID", "-5"),
        ("MINIE_VOICE_DEBUG", "maybe"),
        ("MINIE_LOG_LEVEL", "LOUD"),
        ("MINIE_MAX_DURATION_S", "ten"),
        ("MINIE_LOUDNESS_LUFS", "0"),
        ("MINIE_QUEUE_MAX", "2.5"),
        ("MINIE_OVERLAP", "shuffle"),
        ("MINIE_WEB_PORT", "70000"),
        ("MINIE_WEB_PUBLIC_URL", "localhost:8080"),
        ("MINIE_WEB_PUBLIC_URL", "https://minie.example/sub"),
        ("MINIE_TTS_PITCH", "3"),
        ("MINIE_TTS_PITCH", "0.1"),
        ("MINIE_TTS_KEEP_S", "-1"),
    ],
)
def test_rejects_malformed_values(key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({key: value})


def test_web_ui_settings() -> None:
    assert Settings.from_env({}).web_port is None
    local = Settings.from_env({"MINIE_WEB_PORT": "8080"})
    assert (local.web_host, local.web_url) == ("127.0.0.1", "http://localhost:8080")
    public = Settings.from_env(
        {
            "MINIE_WEB_PORT": "8080",
            "MINIE_WEB_HOST": "0.0.0.0",
            "MINIE_WEB_PUBLIC_URL": "https://minie.example/",
            "MINIE_OAUTH_CLIENT_SECRET": " s3cret ",
        }
    )
    assert public.web_url == "https://minie.example"
    assert public.oauth_client_secret == "s3cret"


def test_speech_settings() -> None:
    off = Settings.from_env({})
    assert off.tts_voice is None
    assert (off.piper, off.tts_pitch, off.tts_keep) == ("piper", 1.3, 300)
    on = Settings.from_env(
        {
            "MINIE_TTS_VOICE": " data/voices/siwis.onnx ",
            "MINIE_PIPER": "/opt/piper",
            "MINIE_TTS_PITCH": "1.25",
            "MINIE_TTS_KEEP_S": "0",
        }
    )
    assert on.tts_voice == Path("data/voices/siwis.onnx")
    assert (on.piper, on.tts_pitch, on.tts_keep) == ("/opt/piper", 1.25, 0)


def test_web_check_needs_the_client_secret() -> None:
    from minie.preflight import _check_web

    assert _check_web(Settings()).ok
    assert not _check_web(Settings(web_port=8080)).ok
    assert _check_web(Settings(web_port=8080, oauth_client_secret="x")).ok


def test_secrets_stay_out_of_repr() -> None:
    settings = Settings.from_env(
        {"DISCORD_TOKEN": "very.secret.token", "MINIE_OAUTH_CLIENT_SECRET": "oauth-secret"}
    )
    assert "secret" not in repr(settings)


def test_missing_token_is_reported() -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        Settings.from_env({}).require_token()
