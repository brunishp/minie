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
| `/leave` | Leaves the voice channel and drops any queued sounds. For members in that channel, and members with Manage Server |
| `/play [sound]` | Plays a sound, with autocomplete on the server's sounds. Without a name, plays a one-second test tone |
| `/stop` | Stops the current sound and clears the queue |
| `/say <text>` | Says up to 200 characters in your voice channel. Only with text to speech set up (see below) |
| `/add <name> <file>` | Adds a sound from an attached audio file: MP3, WAV, Ogg, FLAC, M4A or WebM |
| `/list` | Lists the server's sounds with their lengths |
| `/info <sound>` | Shows who added a sound, when, and its length |
| `/rename <sound> <new name>` | Renames a sound. Allowed for whoever added it, and for members with Manage Server |
| `/remove <sound>` | Removes a sound. Same permission as `/rename` |
| `/panel` | Opens your own grid of sound buttons: 20 per page, with ◀ ▶ to page and ⏹ to stop |

- Playing a sound (`/play` or a panel button) from a voice channel brings the bot there if
  it's in no voice channel, or idle in another one. It's never pulled away while it plays or
  has sounds queued, by `/join` or anything else: the member is told where it's busy instead.
  The cooldown and the interrupt or queue rule apply to clicks too; a click that plays
  nothing doesn't use up the cooldown.
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
| `MINIE_WEB_PORT` | unset | Turns the web UI on, on this port |
| `MINIE_WEB_HOST` | `127.0.0.1` | Address the web UI listens on |
| `MINIE_WEB_PUBLIC_URL` | `http://localhost:<port>` | Address browsers use, and base of the sign-in redirect |
| `MINIE_OAUTH_CLIENT_SECRET` | unset | Required with the web UI (see below); a secret like the token |
| `MINIE_TTS_VOICE` | unset | Piper voice file; turns `/say` on (see below) |
| `MINIE_PIPER` | `piper` | The Piper program |
| `MINIE_TTS_PITCH` | `1.3` | Multiplies the voice's pitch, from 0.5 to 2; its speed stays the same |
| `MINIE_TTS_KEEP_S` | `300` | Seconds Piper stays loaded after its last use; `0` stops it after every sentence |

`interrupt` is the default because it feels immediate, and spamming `/play` can't build up a
backlog of sounds.

## Text to speech

`/say` speaks in a French voice with a raised pitch, which gives a squeaky cartoon voice rather
than anyone's in particular. It's off until you set it up.

1. Install [Piper](https://github.com/OHF-Voice/piper1-gpl) as its own program. Keep to this
   version unless you've checked a newer one with `minie check`: the bot relies on a log line
   the program writes. Python is pinned because `onnxruntime`, which Piper needs, has no 3.15
   builds yet.

   ```bash
   uv tool install --python 3.14 piper-tts==1.8.0
   ```

2. Download the voice (about 60 MB). `data/` is git-ignored.

   ```bash
   uvx --python 3.14 --from piper-tts==1.8.0 python -m piper.download_voices --download-dir data/voices fr_FR-siwis-medium
   ```

3. In `.env`, set `MINIE_TTS_VOICE=data/voices/fr_FR-siwis-medium.onnx`.
4. Run `minie check`, which tries a real sentence, then `minie sync`, and restart the bot.

How it behaves:

- `/say` follows the same rules as a sound: it brings the bot to your channel unless it's busy
  elsewhere, and the cooldown and the interrupt or queue rule apply. To restrict it to some
  roles, use Server Settings › Integrations.
- Mentions and custom emoji are left out, since they'd be read as long strings of digits.
- The log records who used `/say`, never what they said.
- Piper stays loaded for 5 minutes after its last use, then exits and frees its memory. Measured
  on an Apple Silicon Mac: about 235 MiB while loaded, about 1.1 s to load, about 0.1 s for a
  sentence after that. Expect a slower load on a small container.

Licences: Piper is GPL-3.0-or-later and runs as a separate program, so it's neither part of this
repository nor one of its dependencies. The Siwis voice was trained on a
[dataset](https://datashare.is.ed.ac.uk/handle/10283/2353) under CC-BY 4.0.

## Web UI

An optional page to control the bot from a browser: sign in with Discord, pick a server, then
play, stop and manage sounds, with live updates. It runs inside the bot; nothing else to start.

### Turning it on

1. Developer Portal › your application › **OAuth2**:
   - under **Redirects**, add `http://localhost:8080/auth/callback`;
   - **Reset Secret** and copy the client secret.
2. In `.env`:

   ```
   MINIE_WEB_PORT=8080
   MINIE_OAUTH_CLIENT_SECRET=<the client secret>
   ```

3. Restart the bot and open http://localhost:8080. Use exactly the public URL
   (`MINIE_WEB_PUBLIC_URL`): other host names, `127.0.0.1` included, are sent there.

The first visit asks for Discord's consent (your identity and server list). Later visits, and
visits after a restart of the bot, sign you in again without asking.

### What it does

- Every action follows the same rules as the slash commands: playing brings the bot to your
  voice channel unless it's busy elsewhere, the cooldown applies, ⏹ Stop needs you in the bot's
  channel, Leave does too unless you have Manage Server, and only the uploader (or Manage
  Server) can rename or remove a sound.
- If a server restricts a command in Server Settings › Integrations, the web UI refuses the
  matching action (`/play`, `/stop`, `/leave`, `/add`, `/rename`, `/remove`) for everyone,
  and points to the slash command, which Discord checks properly.
- ☆ marks a favourite, remembered in that browser. Favourites come first in the grid.
- **Media keys** let you play sounds while a game has focus. Click **Media keys: off** once to
  turn them on, then:
  - play/pause plays the armed sound (shown on the page, and in the system's Now Playing
    overlay);
  - next and previous move through your favourites (all sounds when none is starred);
  - stop stops the bot.

  The keys go to whichever app played media last: start Spotify and it takes them back; click
  the button again to take them back. They work in Chrome, Edge, Firefox and Safari on the
  desktop.

### Security

- It listens on 127.0.0.1 only: nothing outside this machine reaches it. Opening it to other
  devices needs HTTPS in front (Tailscale, a Cloudflare Tunnel); don't set `MINIE_WEB_HOST` to
  a public address with plain HTTP.
- Discord's access token is used once, to learn who you are and your servers, then revoked.
  Sessions are kept in memory for 7 days, in an HttpOnly cookie; a restart ends them.
- Membership is checked against Discord on every request, so leaving a server ends access to it
  at once. Channels you can't view are never named.
- Writes need the page's own origin and a JSON body (or a custom header for uploads), so other
  sites can't act on your behalf. The access log never records query strings, where the
  sign-in code travels.

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
- FFmpeg and Piper start with a minimal environment, so the bot's token and client secret
  aren't among their variables.
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
outgoing UDP, where IP discovery learns the NAT's public address. The web UI is the exception:
leave it off there until it can sit behind HTTPS.

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
├── commands/                   # slash commands and /panel; they only talk to the services below
├── control.py                  # who may do what, shared by commands, panel and web UI
├── events.py                   # change notifications: sounds and playback status
├── library.py                  # per-server sounds: upload rules, files, frames for playback
├── store.py                    # SQLite metadata, with numbered schema migrations
├── audio.py                    # FFmpeg: probe, loudness, encode once to Ogg/Opus (20 ms frames)
├── playback.py                 # cooldown, then interrupt or queue, above VoicePlayer
├── speech.py                   # text to speech for /say: runs Piper, then FFmpeg
├── tools.py                    # the minimal environment external programs get
├── web/                        # web UI (aiohttp): sign-in, JSON API, live events, static page
└── voice/
    ├── base.py                 # VoicePlayer: the interface the rest of the bot uses
    ├── discordpy.py            # VoicePlayer implemented with discord.py
    └── ogg.py                  # Ogg/Opus reader, independent of discord.py
```

`VoicePlayer` deals only in IDs and 20 ms Opus packets, never in discord.py objects. Another
implementation can replace `voice/discordpy.py` without touching the commands. discord.py's
`VoiceProtocol` hook (`channel.connect(cls=...)`) hands such a replacement the voice server
credentials, after which it runs the voice WebSocket, UDP/RTP transport and DAVE itself.

Uploads, playback and permission rules live in `library.py`, `playback.py` and `control.py`
rather than in the commands, so the web UI reuses them unchanged.

Runtime files live in `data/` (git-ignored):

- `minie.db`: the SQLite database, in WAL mode;
- `sounds/<server ID>/<random ID>.ogg`: the converted sounds;
- `builtin/test-tone.ogg`: generated at startup;
- `tmp/`: uploads being converted.

At startup, the bot empties `tmp/` and deletes sound files the database no longer lists, left
by an upload or a removal cut short. It logs a warning for any listed sound whose file is
missing, and keeps its entry: that means `minie.db` and `sounds/` came from different backups.
Back them up together.
