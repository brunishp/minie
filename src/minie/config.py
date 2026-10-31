"""Runtime configuration, read once from environment variables.

In development the variables come from a git-ignored `.env` file loaded by
`uv run --env-file .env`; under systemd, from the unit's `EnvironmentFile=`.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from minie.playback import Overlap

_MIB = 1024 * 1024


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

    max_upload_bytes: int = 8 * _MIB
    max_duration: float = 10.0
    """Seconds."""
    loudness_lufs: float = -16.0
    true_peak_dbtp: float = -1.5
    max_sounds: int = 100
    """Per guild."""
    cooldown: float = 3.0
    """Seconds a user waits between two sounds; 0 disables it."""
    overlap: Overlap = Overlap.INTERRUPT
    queue_max: int = 5

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
            max_upload_bytes=round(
                _number(env, "MINIE_MAX_UPLOAD_MB", cls.max_upload_bytes / _MIB, 0.1, 100) * _MIB
            ),
            max_duration=_number(env, "MINIE_MAX_DURATION_S", cls.max_duration, 0.5, 300),
            # The ranges loudnorm accepts.
            loudness_lufs=_number(env, "MINIE_LOUDNESS_LUFS", cls.loudness_lufs, -70, -5),
            true_peak_dbtp=_number(env, "MINIE_TRUE_PEAK_DBTP", cls.true_peak_dbtp, -9, 0),
            max_sounds=_integer(env, "MINIE_MAX_SOUNDS", cls.max_sounds, 1, 10_000),
            cooldown=_number(env, "MINIE_COOLDOWN_S", cls.cooldown, 0, 3600),
            overlap=_overlap(env, "MINIE_OVERLAP"),
            queue_max=_integer(env, "MINIE_QUEUE_MAX", cls.queue_max, 1, 50),
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


def _number(env: Mapping[str, str], key: str, default: float, low: float, high: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low:g} and {high:g}, got {raw}")
    return value


def _integer(env: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    value = _number(env, key, default, low, high)
    if not value.is_integer():
        raise ConfigError(f"{key} must be a whole number, got {value:g}")
    return int(value)


def _overlap(env: Mapping[str, str], key: str) -> Overlap:
    raw = env.get(key, "").strip().lower() or Overlap.INTERRUPT
    try:
        return Overlap(raw)
    except ValueError:
        choices = " or ".join(mode.value for mode in Overlap)
        raise ConfigError(f"{key} must be {choices}, got {raw!r}") from None


def _log_level(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key, "").strip().upper() or "INFO"
    if raw not in logging.getLevelNamesMapping():
        raise ConfigError(f"{key} must be a logging level such as DEBUG or INFO, got {raw!r}")
    return raw
