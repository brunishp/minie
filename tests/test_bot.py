from minie.bot import MinieBot
from minie.config import Settings


def test_registers_guild_only_commands() -> None:
    bot = MinieBot(Settings())
    commands = {command.name: command for command in bot.tree.get_commands()}
    assert set(commands) == {"join", "leave", "play"}
    for command in commands.values():
        payload = command.to_dict(bot.tree)
        assert payload["contexts"] == [0]  # guild channels only, not DMs
        assert payload["integration_types"] == [0]  # installed to a server, not a user
