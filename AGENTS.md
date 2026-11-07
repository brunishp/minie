# Notes for coding agents

minie is a Discord soundboard bot: slash commands, voice through discord.py with DAVE (Discord's
end-to-end voice encryption). The README covers setup, commands, settings and the upload
pipeline. This file covers what to keep true when changing the code. Update it in the same
change as anything that makes it wrong.

Code comments explain why, not what, especially for protocol, audio and security choices.

## Commands

```bash
uv sync                                    # dependencies, including dev tools
uv run pytest                              # FFmpeg tests skip when ffmpeg is missing
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run --env-file .env minie check         # voice stack: PyNaCl, davey, libopus, FFmpeg
uv run --env-file .env minie sync          # register slash commands in MINIE_DEV_GUILD_ID
uv run --env-file .env minie run
```

Use uv for everything Python (never pip, venv or poetry). Python is pinned to 3.14: davey has
no 3.15 wheels for Linux ARM or musl. Re-run `minie sync` whenever a command's name, options or
description change. The bot deliberately doesn't sync at startup.

## Invariants

- **Voice seam.** Outside `minie/voice/`, code talks to `VoicePlayer` (`voice/base.py`) with
  guild/channel IDs and Opus frames only, never discord.py voice objects. The goal is to swap
  `voice/discordpy.py` for a hand-written stack later, plugged in through discord.py's
  `VoiceProtocol` (`channel.connect(cls=...)`).
- **20 ms Opus frames.** Every sound is Ogg/Opus, 48 kHz stereo, `FRAME_DURATION_MS = 20`
  (`audio.OPUS_OUTPUT_ARGS`). `voice/ogg.read_opus_frames` refuses anything else: senders emit
  one packet per 20 ms tick. Playback never transcodes.
- **Where the bot goes.** Only `PlaybackService` (`join`, `play`, `leave`) connects, moves or
  disconnects the bot, under its per-guild lock, and never away from a channel where it plays
  or has a queue. Callers check permissions first with `Soundboard.target_channel`, and defer
  when it says a connection is needed. Only members in its channel, or with Manage Server, can
  make it leave (`Soundboard.can_leave`).
- **Rules outside the commands.** Upload rules live in `library.py`, playback rules (cooldown,
  interrupt or queue) in `playback.py`, and who may do what in `control.py`: `Soundboard`,
  which sees members as a `Caller` and asks Discord questions through the `Presence` seam.
  Slash commands, panel buttons and the web UI all go through `Soundboard`, and stay thin.
- **Change events.** The library publishes `Topic.SOUNDS` and `PlaybackService` publishes
  `Topic.STATUS` on every change (`events.py`). Panels and web pages listen, so a change from
  any source reaches all of them; nothing calls them directly. Events carry no state:
  listeners read the current state themselves.
- **Command errors.** Raise `UserError` (`errors.py`) for anything the user should read;
  `bot._on_command_error` shows it, and logs every other exception with a generic reply. Reply
  with `commands.common.reply` (ephemeral, works before or after deferring). Defer anything
  that can exceed Discord's 3 s deadline (voice connects, uploads).
- **Panel buttons** (`commands/panel.py`) are `DynamicItem`s whose state lives in the
  `custom_id` (`minie:<action>:…`, at most 100 characters, unique within a message), so they
  survive restarts. New button classes go in `panel.BUTTONS`, which `bot.setup_hook`
  registers. discord.py only logs exceptions from their callbacks, so they go through
  `_handle`, which calls `commands.common.report_error`. Build a new `View` for every draw
  (`panel.render`).
- **Ephemeral panels** can only be edited through an interaction token valid 15 minutes.
  `PanelRegistry` keeps those interactions in memory only: they're credentials, never to be
  stored or logged.
- **Web UI** (`web/`, aiohttp in the bot's process and event loop; off unless
  `MINIE_WEB_PORT` is set):
  - Every action goes through `Soundboard`, after `api._caller` checks membership against
    Discord (never trust the guild list from sign-in), and `api._check_command`, which refuses
    any action whose slash command a server restricts in Integrations. The web UI must never
    allow more than Discord would.
  - Sessions and OAuth tokens are credentials: memory only, never logged. The access token is
    revoked right after sign-in. The access log prints paths without query strings.
  - Writes need `Origin` equal to `MINIE_WEB_PUBLIC_URL`, and a JSON body or the `X-Minie`
    header; never add CORS. Other host names are redirected or refused (DNS rebinding).
  - Discord IDs go out as strings (JavaScript loses precision above 2^53). Error messages pass
    through `api._readable`, which names only channels the member can view.
  - The page is plain ES modules with no build step. CSP forbids inline script; put text in
    with `textContent`, never `innerHTML`.
- **Uploads are hostile input to FFmpeg.** Every ffprobe or ffmpeg call that reads an upload
  passes `audio._INPUT_RESTRICTIONS` (demuxer allowlist, `file` protocol only) and forces the
  probed demuxer with `-f`. Check durations on decoded output, not on container metadata.
  Conversions are bounded: timeout, `-t`, `MAX_CONCURRENT_CONVERSIONS`.
- **Text to speech** (`speech.py`; `/say` only exists when `MINIE_TTS_VOICE` is set):
  - Piper is a separate program, like FFmpeg, never imported: its GPL code stays out of this
    repository and its dependencies. A worker script of ours importing it would undo that.
  - `PiperWorker` drives its command-line loop: one line in, one WAV out, and the
    `Wrote <path>` line on stderr is the only completion signal. `minie check` tries a real
    sentence, so a Piper update that changes this shows up there. Only files in the work
    directory count, since the bot reads and deletes them. Anything unfinished kills the
    process, or the next sentence would read the wrong answer.
  - Text is untrusted: `speakable` flattens it to one line (a line break would be a second
    request) and refuses punctuation alone (Piper's program exits with an error on it). Never
    log the text.
  - A long install path breaks Piper's espeak-ng: it worked at 107 characters and failed at
    184, with an error naming a path on Piper's build machine (`phontab`). The default
    `uv tool` location is fine; a deep custom `UV_TOOL_DIR` isn't.
- **Secrets.** The token and the OAuth client secret come only from the environment (`.env` is
  git-ignored). Never log tokens or voice keys; `log.RedactSecrets` masks discord.py's gateway
  dumps. Keep `repr=False` on secret settings. Start every external program with
  `env=tools.tool_environment()`, an allowlist. That's defence in depth, not isolation: a
  compromised program under the same user could still read `.env` or the bot's
  `/proc/<pid>/environ`, so real isolation needs a separate user or sandbox.
- **Least privilege.** Intents are `Intents.none()` plus `guilds` and `voice_states`. Invite
  permissions are View Channel, Connect and Speak. No privileged intents, no message content.
  Web sign-in asks only for the `identify` and `guilds` scopes.
- **Storage.** Schema changes append a migration to `store._MIGRATIONS`; never edit an existing
  one. Uniqueness and limits are enforced in SQL, not just checked beforehand. Files are
  written to a temporary name, then renamed, and named after the sound's ID, never its name.
  `SoundLibrary.clean_up` runs at startup and deletes unlisted files, so the bot must be the
  only process writing to the data directory: a V2 web UI uploads through it, not beside it.
- **No blocking in coroutines.** Ruff's ASYNC rules are on. Use `asyncio.create_subprocess_exec`
  and `asyncio.to_thread`. Small SQLite queries on the loop thread are the one deliberate
  exception (see `store.py`).

## Discord platform facts (as of 2026-10)

- DAVE is mandatory for voice since 2026-03-01; connections without it close with code 4017.
  discord.py ≥ 2.7 needs the `voice` extra (PyNaCl, davey) and libopus.
- From 2026-11-16, the gateway sends channels the bot can't view with a placeholder name
  (`___hidden___`) and an overwrite denying View Channel. Name channels by mention, never by
  name, and check `permissions_for(guild.me)` before acting on one.
- Guild commands update instantly, global ones don't; development uses the guild.

## Tests

- `tests/fakes.py` has `FakeVoice` (an in-memory `VoicePlayer`), `FakePresence`, a clock and
  an Ogg/Opus writer. Use them for anything above the voice and Discord seams; tests import
  them with `from fakes import …`.
- `tests/test_voice_discordpy.py` runs the discord.py adapter against discord.py's real sender
  thread with a fake connection.
- `tests/test_panel.py` checks panel layouts and button ID round-trips, and fakes the few
  `Interaction` members `PanelRegistry` uses.
- `tests/web_harness.py` runs the web UI on the fakes, against `FakeDiscord`, a fake OAuth2
  server; `Harness.sign_in` goes through the real sign-in flow.
- FFmpeg tests generate their inputs with lavfi sources and skip when ffmpeg is missing.
- pytest-asyncio runs in auto mode: `async def test_…` needs no marker.

## Roadmap (don't implement unasked, but don't block)

- Idea: leaving after some minutes alone in a channel. It would bring back join sounds, so
  weigh that first.
- Deployment: a systemd unit and update script for the Proxmox LXC container, on the
  `deploy-service` branch until the maintainer decides to run the bot as a service.
- `/say` in the web UI: a text box, with `_check_command(..., "say")` like the other actions.
- Web UI, next steps:
  - Play on this device: a second `target` in `static/app.js`. Serve the Ogg/Opus file at
    `GET …/sounds/{id}/audio` (immutable: IDs are random and files never change), decode with
    Web Audio, and pick the output device with `setSinkId()` (Chrome, Edge, Firefox 116+,
    Safari 18.4+). No cooldown or queue: those protect the shared voice channel. Users route
    that output into games themselves (VB-CABLE, BlackHole, PipeWire). Safari's Ogg/Opus
    support is unreliable; a WebM copy (`-c copy`, no re-encoding) at upload would cover it.
  - Reach from other devices: HTTPS in front (Tailscale, Cloudflare Tunnel), with
    `MINIE_WEB_PUBLIC_URL` set; cookies become `Secure` automatically.
  - A finer reading of Integrations overwrites (roles, users, channels) if refusing outright
    gets in the way.
- Mixer for overlapping sounds (decode to PCM, sum with clipping handling, re-encode 20 ms Opus
  frames). It outputs `OpusFrames` like any other source.
- Protocol track: observe the voice and DAVE negotiation (`MINIE_VOICE_DEBUG=1`), then
  reimplement the transport (voice WebSocket, UDP IP discovery, RTP/Opus, AEAD with AES-256-GCM
  or XChaCha20-Poly1305) as a new `VoicePlayer`, with DAVE through davey or dave.py.
  The only AEAD mode discord.py implements is `aead_xchacha20_poly1305_rtpsize`; AES-256-GCM
  (`aead_aes256_gcm_rtpsize`) would be new code.
- Voice commands ("minie airhorn"), once the protocol track has a receive path. discord.py
  only sends audio, and `discord-ext-voice-recv` (no commit since 2025-06) has no DAVE
  decryption merged.
  - Receive behind a `VoiceListener` seam beside `VoicePlayer`, yielding each speaker's Opus
    frames: UDP, transport AEAD, RTP, mapping streams (SSRC) to users,
    `davey.DaveSession.decrypt` with per-sender keys, Opus decode.
  - Opt-in per member. Drop everyone else's packets before DAVE decryption, so their voice is
    never heard. Store no audio. Joining undeafened shows members the bot listens.
  - Voice activity detection (Silero or WebRTC) picks out short utterances. Recognition runs
    on CPU: Vosk with a grammar of "minie" plus the sounds' spoken forms (`bad_horn-2` →
    "bad horn two"), or faster-whisper tiny/base in int8 with fuzzy matching. It runs outside
    the event loop, from an optional dependency group.
  - Recognised commands go through `PlaybackService.play`, so the cooldown and the busy rule
    apply.
  - First benchmark recognition offline on the target container, with recorded clips. Check
    Discord's Developer Policy on processing voice before enabling it on any server.

## Git

English commit messages with an imperative subject line. Work on a branch, not on `main`.
