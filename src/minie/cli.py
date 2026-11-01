"""Command-line entry point: `minie run`, `minie sync` and `minie check`."""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

import discord

from minie import audio, preflight
from minie.bot import MinieBot, sync_commands
from minie.config import ConfigError, Settings
from minie.log import setup_logging

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"minie: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, voice_debug=settings.voice_debug)

    try:
        match args.command:
            case "check":
                return _check(settings)
            case "sync":
                return _sync(settings, global_=args.global_, clear=args.clear)
            case _:
                return _run(settings)
    except ConfigError as exc:
        _log.error("%s", exc)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minie", description="Discord soundboard bot.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="connect to Discord and answer slash commands")
    commands.add_parser("check", help="check the voice stack (PyNaCl, davey, libopus, FFmpeg)")
    sync = commands.add_parser("sync", help="register the slash commands with Discord")
    scope = sync.add_mutually_exclusive_group()
    scope.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="register in every guild (slow to propagate) instead of MINIE_DEV_GUILD_ID",
    )
    scope.add_argument(
        "--clear", action="store_true", help="remove the commands registered in MINIE_DEV_GUILD_ID"
    )
    return parser


def _check(settings: Settings) -> int:
    checks = preflight.run_checks(settings)
    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"{'ok' if check.ok else 'FAIL':<4}  {check.name:<{width}}  {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def _run(settings: Settings) -> int:
    token = settings.require_token()
    checks = preflight.run_checks(settings)
    for check in checks:
        if check.ok:
            _log.info("%s %s", check.name, check.detail)
        else:
            _log.error("%s: %s", check.name, check.detail)
    if not all(check.ok for check in checks):
        _log.error("Voice stack incomplete, not starting")
        return 1

    bot = MinieBot(settings)
    try:
        audio.ensure_test_tone(settings.ffmpeg, bot.test_tone)
    except audio.AudioError as exc:
        _log.error("Could not create the test sound: %s", exc)
        return 1
    try:
        bot.library.clean_up()
    except OSError:
        # Leftover files waste space but break nothing: not a reason to stay offline.
        _log.warning("Could not clean up the data directory", exc_info=True)

    try:
        asyncio.run(_serve(bot, token))
    except KeyboardInterrupt, asyncio.CancelledError:
        pass
    except discord.LoginFailure:
        _log.error("Discord rejected the token; reset it in the Developer Portal")
        return 1
    return 0


async def _serve(bot: MinieBot, token: str) -> None:
    # systemd stops services with SIGTERM. Handle it like Ctrl-C, so voice connections are
    # closed cleanly instead of the bot vanishing from the channel only after a timeout.
    main_task = asyncio.current_task()
    assert main_task is not None
    with contextlib.suppress(NotImplementedError):  # no signal handlers on Windows
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    async with bot:
        await bot.start(token)


def _sync(settings: Settings, *, global_: bool, clear: bool) -> int:
    token = settings.require_token()
    guild_id = None if global_ else settings.dev_guild_id
    if not global_ and guild_id is None:
        raise ConfigError("MINIE_DEV_GUILD_ID is not set; set it or pass --global")
    bot = MinieBot(settings)

    async def sync() -> None:
        async with bot:
            # login() is HTTP only: registering commands doesn't need a gateway session.
            await bot.login(token)
            await sync_commands(bot.tree, guild_id=guild_id, clear=clear)

    try:
        asyncio.run(sync())
    except discord.LoginFailure:
        _log.error("Discord rejected the token; reset it in the Developer Portal")
        return 1
    except discord.Forbidden as exc:
        _log.error(
            "Discord refused the sync (%s). Is the bot in guild %s, invited with the "
            "applications.commands scope?",
            exc.text,
            guild_id,
        )
        return 1
    return 0
