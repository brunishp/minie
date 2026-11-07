"""What external programs inherit from the bot."""

import os


def tool_environment() -> dict[str, str]:
    """Enough to run, and none of the bot's secrets: an allowlist, so that a secret added later
    stays out too. FFmpeg parses hostile uploads and Piper reads text anyone can type, so
    neither should find the token by reading its own environment.
    """
    return {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR") if name in os.environ}
