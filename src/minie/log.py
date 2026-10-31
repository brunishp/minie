"""Logging setup: one stderr handler, optional voice protocol tracing, secrets masked."""

import logging
import sys
from collections.abc import Mapping
from typing import Any

# discord.py logs the voice gateway (JSON and binary DAVE/MLS opcodes) in discord.gateway,
# the connection state machine (IP discovery, DAVE transitions) in discord.voice_state, and
# the 20 ms sender thread in discord.player.
VOICE_LOGGERS = ("discord.gateway", "discord.voice_state", "discord.voice_client", "discord.player")

_SECRET_KEYS = frozenset({"token", "secret_key"})

# discord.gateway also carries the main gateway. At DEBUG it dumps every dispatched event
# and heartbeat, which buries the voice traffic.
_MAIN_GATEWAY_CHATTER = frozenset(
    {
        "For Shard ID %s: WebSocket Event: %s",
        "Keeping shard ID %s websocket alive with sequence %s.",
    }
)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: "<redacted>" if k in _SECRET_KEYS else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class RedactSecrets(logging.Filter):
    """Mask voice tokens and transport keys in discord.py's gateway frame dumps.

    At DEBUG, discord.py logs voice gateway frames verbatim, including the voice token
    (IDENTIFY, VOICE_SERVER_UPDATE) and the transport encryption key (`secret_key` in
    SESSION_DESCRIPTION). The record gets a redacted copy: the original dicts are still
    in use by discord.py when the record is emitted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, Mapping):
            record.args = _redact(args)
        elif isinstance(args, tuple) and any(isinstance(a, Mapping | list) for a in args):
            record.args = tuple(_redact(a) for a in args)
        return True


class DropMainGatewayChatter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.msg not in _MAIN_GATEWAY_CHATTER


def setup_logging(level: str, *, voice_debug: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    gateway = logging.getLogger("discord.gateway")
    gateway.addFilter(RedactSecrets())
    if voice_debug:
        for name in VOICE_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        if root.level > logging.DEBUG:
            gateway.addFilter(DropMainGatewayChatter())
