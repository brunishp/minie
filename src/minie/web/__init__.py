"""The web UI: control the bot from a browser, signed in with Discord.

It runs in the bot's process and event loop, where playback and voice live, and goes through
the same `Soundboard` rules as the slash commands.
"""

from minie.web.app import create_app, start
from minie.web.core import Services

__all__ = ["Services", "create_app", "start"]
