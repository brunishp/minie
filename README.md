# minie

A Discord soundboard bot. Slash commands only; voice goes through discord.py with DAVE, the
end-to-end encryption Discord has required on every voice connection since March 2026.

Each server gets its own sound library. Uploaded files are checked, normalised to the same
loudness and converted once to Opus, so playback needs no transcoding.

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
| `/join` | Joins your voice channel, self-deafened since it never listens. Rarely needed: playing a sound brings the bot along |
| `/leave` | Leaves the voice channel and drops any queued sounds |
| `/play [sound]` | Plays a sound, with autocomplete on the server's sounds. Without a name, plays a one-second test tone |
| `/stop` | Stops the current sound and clears the queue |
| `/add <name> <file>` | Adds a sound from an attached audio file: MP3, WAV, Ogg, FLAC, M4A or WebM |
| `/list` | Lists the server's sounds with their lengths |
| `/remove <sound>` | Removes a sound. Allowed for whoever added it, and for members with Manage Server |
| `/panel` | Opens your own grid of sound buttons: 20 per page, with ◀ ▶ to page and ⏹ to stop |

- Playing a sound (`/play` or a panel button) from a voice channel brings the bot there if
  it's in no voice channel, or idle in another one. It's never pulled away while it plays or
  has sounds queued, by `/join` or anything else: the member is told where it's busy instead.
  The cooldown and the interrupt or queue rule apply to clicks too.
- `/stop` and the panel's ⏹ only work for members in the bot's voice channel.
- Discord plays its join sound whenever the bot (or anyone) joins a channel. Each member's
  app plays it, and no bot can turn it off. The bot stays connected once it has joined, so
  the sound is rare. Members can turn it off for themselves under User Settings >
  Notifications ("User Join" and "User Leave"), for everyone who joins.
- A panel is private: only whoever opened it sees it, and each member pages on their own.
  Clicking a sound shows nothing; only problems (cooldown, wrong channel) get a private message.
  A panel follows `/add` and `/remove` as they happen, for about 15 minutes after its last
  use (the limit Discord puts on editing it); after that it catches up on the next click. It
  survives bot restarts, but not reloading Discord: run `/panel` again.
- Sound names are 1 to 32 characters: letters, digits, `-` and `_`. Case is ignored.
- Anyone can use `/add` by default. To restrict it to some roles, open Server Settings >
  Integrations and pick the bot. No code change is needed.
- Replies are only visible to whoever ran the command, and never ping anyone.

## Settings

All optional, in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `MINIE_MAX_UPLOAD_MB` | `8` | Largest file accepted, as uploaded |
| `MINIE_MAX_DURATION_S` | `10` | Longest clip accepted, in seconds |
| `MINIE_LOUDNESS_LUFS` | `-16` | Loudness every sound is normalised to |
| `MINIE_TRUE_PEAK_DBTP` | `-1.5` | Ceiling for peaks after normalisation |
| `MINIE_MAX_SOUNDS` | `100` | Sounds per server |
| `MINIE_COOLDOWN_S` | `3` | Seconds between two sounds from the same member; `0` disables it |
| `MINIE_OVERLAP` | `interrupt` | When a sound is already playing: `interrupt` cuts it off, `queue` plays the new one afterwards |
| `MINIE_QUEUE_MAX` | `5` | Sounds that can wait in `queue` mode |
| `MINIE_DATA_DIR` | `data` | Where the database and sounds are stored |

`interrupt` is the default because it feels immediate, and spamming `/play` can't build up a
backlog of sounds.

## How uploads are processed

1. Before downloading, the bot checks the name, the file size and the server's sound count.
2. ffprobe identifies the format and the length the file claims. Clips over the limit are
   refused.
3. FFmpeg measures the loudness (loudnorm, first pass) and refuses silent clips.
4. A second pass applies one constant gain to reach the target, and encodes to Ogg/Opus:
   48 kHz, stereo, 96 kb/s, 20 ms frames. loudnorm only compresses when that gain would push
   peaks above the true peak ceiling.
5. The real length is checked on the encoded result, whatever the file claimed. The decoder
   never reads more than one second past the limit.

Uploaded files are untrusted input to FFmpeg, so:

- Only a fixed list of demuxers may read an upload (FFmpeg's `-format_whitelist`), and FFmpeg
  gets no network access (`-protocol_whitelist file`). Some formats, such as HLS playlists, make
  FFmpeg open other files or URLs named inside them; those are refused before they're parsed.
- Each FFmpeg run is killed after 60 s, and at most two conversions run at once.
- Tags and chapters are stripped, and files are stored under random names.

FFmpeg's decoders still parse the upload. The container the bot runs in is the isolation
boundary for that.

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
├── library.py                  # per-server sounds: upload rules, files, frames for playback
├── store.py                    # SQLite metadata, with numbered schema migrations
├── audio.py                    # FFmpeg: probe, loudness, encode once to Ogg/Opus (20 ms frames)
├── playback.py                 # cooldown, then interrupt or queue, above VoicePlayer
└── voice/
    ├── base.py                 # VoicePlayer: the interface the rest of the bot uses
    ├── discordpy.py            # VoicePlayer implemented with discord.py
    └── ogg.py                  # Ogg/Opus reader, independent of discord.py
```

`VoicePlayer` deals only in IDs and 20 ms Opus packets, never in discord.py objects. Another
implementation can replace `voice/discordpy.py` without touching the commands. discord.py's
`VoiceProtocol` hook (`channel.connect(cls=...)`) hands such a replacement the voice server
credentials, after which it runs the voice WebSocket, UDP/RTP transport and DAVE itself.

Uploads and playback rules live in `library.py` and `playback.py` rather than in the commands,
so the planned web UI can reuse them unchanged.

Runtime files live in `data/` (git-ignored):

- `minie.db`: the SQLite database, in WAL mode;
- `sounds/<server ID>/<random ID>.ogg`: the converted sounds;
- `builtin/test-tone.ogg`: generated at startup;
- `tmp/`: uploads being converted.

Back up `minie.db` and `sounds/` together.
