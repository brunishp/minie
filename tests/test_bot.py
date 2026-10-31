from minie.bot import MinieBot
from minie.config import Settings


def test_registers_guild_only_commands() -> None:
    bot = MinieBot(Settings())
    commands = {command.name: command for command in bot.tree.get_commands()}
    assert set(commands) == {"join", "leave", "play", "stop", "add", "list", "remove"}
    for command in commands.values():
        payload = command.to_dict(bot.tree)
        assert payload["contexts"] == [0]  # guild channels only, not DMs
        assert payload["integration_types"] == [0]  # installed to a server, not a user


def test_sound_options_offer_autocomplete() -> None:
    bot = MinieBot(Settings())
    for name in ("play", "remove"):
        command = bot.tree.get_command(name)
        assert command is not None
        options = command.to_dict(bot.tree)["options"]
        assert options[0]["name"] == "sound"
        assert options[0]["autocomplete"] is True
