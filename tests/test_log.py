import logging
from typing import Any

from minie.log import DropMainGatewayChatter, RedactSecrets


def record(msg: str, *args: Any) -> logging.LogRecord:
    return logging.LogRecord("discord.gateway", logging.DEBUG, __file__, 1, msg, args, None)


def test_redacts_the_transport_key_without_touching_the_frame() -> None:
    frame = {
        "op": 4,
        "d": {"mode": "aead_xchacha20_poly1305_rtpsize", "secret_key": [1, 2, 3], "dave": 1},
    }
    log_record = record("Voice websocket frame received: %s", frame)
    RedactSecrets().filter(log_record)
    message = log_record.getMessage()
    assert "<redacted>" in message
    assert "[1, 2, 3]" not in message
    assert "aead_xchacha20_poly1305_rtpsize" in message
    assert frame["d"]["secret_key"] == [1, 2, 3]  # discord.py still needs the real key


def test_redacts_tokens_nested_in_positional_arguments() -> None:
    event = {"t": "VOICE_SERVER_UPDATE", "d": {"token": "voice-token", "endpoint": "x"}}
    log_record = record("For Shard ID %s: WebSocket Event: %s", None, event)
    RedactSecrets().filter(log_record)
    assert "voice-token" not in log_record.getMessage()


def test_drops_main_gateway_chatter_only() -> None:
    chatter = DropMainGatewayChatter()
    assert not chatter.filter(record("For Shard ID %s: WebSocket Event: %s", None, {}))
    assert chatter.filter(record("Voice websocket frame received: %s", {"op": 2}))
