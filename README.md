# minie

A Discord soundboard bot. Slash commands only; voice goes through discord.py with DAVE, the
end-to-end encryption Discord has required on every voice connection since March 2026.

Current state: `/join`, `/leave` and `/play` with a built-in test sound. Uploads, the per-server
sound library, `/list` and the anti-spam safeguards come next.

## Requirements

- [uv](https://docs.astral.sh/uv/). It also installs the Python version the project pins (3.14).
- FFmpeg built with libopus, and libopus itself:
  - macOS: `brew install ffmpeg opus`
  - Debian or Ubuntu, including the LXC container: `sudo apt install ffmpeg libopus0`

`uv sync` installs the Python side: `discord.py[voice]`, which pulls in PyNaCl (transport
encryption) and davey (DAVE/MLS). The `libffi-dev libnacl-dev python3-dev` packages in discord.py's
own install guide are only needed to build PyNaCl from source; its wheels bundle libsodium.

The project pins Python 3.14 rather than 3.15 because davey 0.1.7 ships no 3.15 wheels for Linux
on ARM or for musl (Alpine). There, uv would have to build it from source with a Rust toolchain.

## 1. Create the Discord application

1. In the [Developer Portal](https://discord.com/developers/applications), click
   **New Application**.
2. **Installation** page: under Installation Contexts keep only **Guild Install**, and set
   **Install Link** to **None**. Discord won't let a private bot keep its default install link.
3. **Bot** page:
   - **Reset Token** and copy the token into `.env` (next section). Anyone holding it controls
     the bot; reset it if it ever leaks.
   - Turn **Public Bot** off, so that only you can add the bot to servers.
   - Leave every **Privileged Gateway Intent** off. Slash commands don't need message content.

## 2. Invite the bot

Open this URL, with your **Application ID** from the General Information page:

```
https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot+applications.commands&permissions=3146752&integration_type=0
```

- Scopes: `bot` (the bot user, which joins voice channels) and `applications.commands` (slash
  commands).
- Permissions `3146752` = View Channels (`1 << 10`) + Connect (`1 << 20`) + Speak (`1 << 21`).
  Nothing else: replies to slash commands don't need Send Messages.

If a voice channel's permission overwrites take one of these away, `/join` says which one is
missing.

## 3. Configure

```bash
cp .env.example .env
```

Set `DISCORD_TOKEN` and `MINIE_DEV_GUILD_ID` in `.env`. To get the server ID, turn on Developer
Mode (Discord settings > Advanced), then right-click the server and choose Copy Server ID.
`.env` is git-ignored.

## 4. Install, check, register, run

```bash
uv sync
```

```bash
uv run --env-file .env minie check
```

```bash
uv run --env-file .env minie sync
```

```bash
uv run --env-file .env minie run
```

- `minie check` prints the versions of Python, discord.py, PyNaCl, davey, libopus, FFmpeg and
  ffprobe, and fails if any is missing. `minie run` runs the same check and refuses to start on a
  failure, rather than failing later at the first `/join` (Discord closes non-DAVE voice
  connections with code 4017).
- `minie sync` registers the commands in `MINIE_DEV_GUILD_ID` only, where updates show up
  instantly. Run it again whenever the commands change. It isn't done at every start, to stay
  clear of Discord's daily limit on command creation. To go global later, run
  `minie sync --global`, then `minie sync --clear` to remove the guild copies. Otherwise every
  command shows up twice in that server.
- Stop the bot with Ctrl-C (or SIGTERM under systemd). It leaves voice channels cleanly.

## Commands

| Command | What it does |
|---|---|
| `/join` | Joins your voice channel, self-deafened since it never listens |
| `/leave` | Leaves the voice channel |
| `/play` | Plays a one-second test tone. You must be in the bot's channel. A new sound cuts off the current one |

Replies are only visible to whoever ran the command.

## Logs

- `MINIE_LOG_LEVEL` sets the overall level (default `INFO`).
- `MINIE_VOICE_DEBUG=1` traces the voice stack at DEBUG level:
  - voice gateway opcodes, both JSON and binary DAVE/MLS messages;
  - choice of encryption mode;
  - UDP IP discovery;
  - DAVE transitions and epochs.

  The voice token and the transport key (`secret_key`) are redacted. The main gateway's event
  dumps are hidden in this mode; `MINIE_LOG_LEVEL=DEBUG` shows everything.

## Running in the LXC container

Same steps on Debian or Ubuntu: install the apt packages and uv, clone the repository, then
`uv sync --frozen --no-dev`. Keep `.env` readable by the bot's user only (`chmod 600 .env`). No
inbound ports are needed, even behind NAT: the gateway is an outgoing WebSocket, and voice is
outgoing UDP, where IP discovery learns the NAT's public address.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

```bash
uv run pyright
```

## Layout

```
src/minie/
├── cli.py, config.py, log.py   # entry point, environment settings, logging (secrets redacted)
├── preflight.py                # voice stack check run by `minie check` and `minie run`
├── bot.py                      # discord.Client, command tree, error handling, command sync
├── commands/                   # slash commands; they only talk to the services below
├── audio.py                    # FFmpeg: everything is encoded once to Ogg/Opus, 48 kHz, 20 ms frames
└── voice/
    ├── base.py                 # VoicePlayer: the interface the rest of the bot uses
    ├── discordpy.py            # VoicePlayer implemented with discord.py
    └── ogg.py                  # Ogg/Opus reader, independent of discord.py
```

`VoicePlayer` deals only in IDs and 20 ms Opus packets, never in discord.py objects. Another
implementation can replace `voice/discordpy.py` without touching the commands. discord.py's
`VoiceProtocol` hook (`channel.connect(cls=...)`) hands such a replacement the voice server
credentials, after which it runs the voice WebSocket, UDP/RTP transport and DAVE itself.

Runtime files live in `data/` (git-ignored). For now that's only the generated test tone.
