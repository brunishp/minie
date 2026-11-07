from datetime import UTC, datetime
from pathlib import Path

from minie.bot import MinieBot
from minie.commands.sounds import _format_info
from minie.config import Settings
from minie.speech import MAX_TEXT
from minie.store import Sound


def test_registers_guild_only_commands() -> None:
    bot = MinieBot(Settings())
    commands = {command.name: command for command in bot.tree.get_commands()}
    assert set(commands) == {
        "join",
        "leave",
        "play",
        "stop",
        "add",
        "list",
        "info",
        "rename",
        "remove",
        "panel",
    }
    for command in commands.values():
        payload = command.to_dict(bot.tree)
        assert payload["contexts"] == [0]  # guild channels only, not DMs
        assert payload["integration_types"] == [0]  # installed to a server, not a user


def test_sound_options_offer_autocomplete() -> None:
    bot = MinieBot(Settings())
    for name in ("play", "info", "rename", "remove"):
        command = bot.tree.get_command(name)
        assert command is not None
        options = command.to_dict(bot.tree)["options"]
        assert options[0]["name"] == "sound"
        assert options[0]["autocomplete"] is True


def test_info_uses_mentions_and_local_timestamps() -> None:
    added = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    text = _format_info(Sound("id", 1, "horn", 42, 2400, added))
    stamp = int(added.timestamp())
    assert text == f"`horn` · 2.4 s\nAdded by <@42> on <t:{stamp}:D> (<t:{stamp}:R>)"


def test_say_only_exists_with_a_voice() -> None:
    assert MinieBot(Settings()).tree.get_command("say") is None
    bot = MinieBot(Settings(tts_voice=Path("voice.onnx")))
    command = bot.tree.get_command("say")
    assert command is not None
    option = command.to_dict(bot.tree)["options"][0]
    assert (option["name"], option["min_length"], option["max_length"]) == ("text", 1, MAX_TEXT)
