"""Runtime configuration, read once from environment variables.

In development the variables come from a git-ignored `.env` file loaded by
`uv run --env-file .env`; under systemd, from the unit's `EnvironmentFile=`.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """A configuration value is missing or malformed."""


@dataclass(frozen=True, kw_only=True)
class Settings:
    # repr=False keeps the token out of logs and tracebacks that print the settings.
    token: str | None = field(default=None, repr=False)
    dev_guild_id: int | None = None
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    voice_debug: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    opus_library: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        return cls(
            token=env.get("DISCORD_TOKEN", "").strip() or None,
            dev_guild_id=_snowflake(env, "MINIE_DEV_GUILD_ID"),
            data_dir=Path(env.get("MINIE_DATA_DIR", "").strip() or "data"),
            log_level=_log_level(env, "MINIE_LOG_LEVEL"),
            voice_debug=_bool(env, "MINIE_VOICE_DEBUG"),
            ffmpeg=env.get("MINIE_FFMPEG", "").strip() or "ffmpeg",
            ffprobe=env.get("MINIE_FFPROBE", "").strip() or "ffprobe",
            opus_library=env.get("MINIE_OPUS_LIBRARY", "").strip() or None,
        )

    def require_token(self) -> str:
        if self.token is None:
            raise ConfigError("DISCORD_TOKEN is not set (see .env.example)")
        return self.token


def _snowflake(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) == 0:
        raise ConfigError(f"{key} must be a Discord ID (digits only), got {raw!r}")
    return int(raw)


def _bool(env: Mapping[str, str], key: str) -> bool:
    raw = env.get(key, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{key} must be a boolean (1/0, true/false), got {raw!r}")


def _log_level(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key, "").strip().upper() or "INFO"
    if raw not in logging.getLevelNamesMapping():
        raise ConfigError(f"{key} must be a logging level such as DEBUG or INFO, got {raw!r}")
    return raw
