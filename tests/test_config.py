from pathlib import Path

import pytest

from minie.config import ConfigError, Settings


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


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MINIE_DEV_GUILD_ID", "my-server"),
        ("MINIE_DEV_GUILD_ID", "-5"),
        ("MINIE_VOICE_DEBUG", "maybe"),
        ("MINIE_LOG_LEVEL", "LOUD"),
    ],
)
def test_rejects_malformed_values(key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({key: value})


def test_token_stays_out_of_repr() -> None:
    settings = Settings.from_env({"DISCORD_TOKEN": "very.secret.token"})
    assert "secret" not in repr(settings)


def test_missing_token_is_reported() -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        Settings.from_env({}).require_token()
