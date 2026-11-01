"""Each guild's sound library: uploads in, 20 ms Opus frames out.

Kept apart from the slash commands so the web UI adds and plays sounds through the same rules.
Every change to a guild's sounds is published as a `Topic.SOUNDS` event.
"""

import asyncio
import logging
import re
import secrets
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from minie import audio
from minie.config import Settings
from minie.errors import UserError
from minie.events import EventHub, Topic
from minie.store import GuildFullError, Sound, SoundExistsError, SoundStore
from minie.voice.ogg import read_opus_frames

_log = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")
NAME_RULES = "1 to 32 characters: letters, digits, - and _"
# Conversions are CPU-bound; a small container shouldn't run a pile of them at once.
MAX_CONCURRENT_CONVERSIONS = 2


class SoundNotFound(UserError):
    pass


def normalise_name(raw: str) -> str:
    """Sound names are compared ignoring case, so they're stored in lowercase."""
    name = raw.strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise UserError(f"Sound names are {NAME_RULES}.")
    return name


class SoundLibrary:
    def __init__(
        self, store: SoundStore, settings: Settings, events: EventHub | None = None
    ) -> None:
        self._store = store
        self._settings = settings
        self._events = events or EventHub()
        self._sounds_dir = settings.data_dir / "sounds"
        self._tmp_dir = settings.data_dir / "tmp"
        self._conversions = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)

    def find(self, guild_id: int, name: str) -> Sound:
        sound = self._store.get(guild_id, name.strip().lower())
        if sound is None:
            raise SoundNotFound("There's no sound by that name, see /list.")
        return sound

    def sounds(self, guild_id: int) -> list[Sound]:
        return self._store.sounds(guild_id)

    def search(self, guild_id: int, text: str) -> list[Sound]:
        return self._store.search(guild_id, text.strip().lower())

    def check_can_add(self, guild_id: int, name: str, size: int) -> str:
        """Cheap checks, made before downloading the attachment. Returns the normalised name."""
        name = normalise_name(name)
        limit = self._settings.max_upload_bytes
        if size > limit:
            raise UserError(
                f"That file is {size / 2**20:.1f} MiB, the limit is {limit / 2**20:g} MiB."
            )
        if self._store.get(guild_id, name) is not None:
            raise SoundExistsError(f"There's already a sound called `{name}`.")
        if self._store.count(guild_id) >= self._settings.max_sounds:
            raise GuildFullError(
                f"This server already has {self._settings.max_sounds} sounds, the maximum."
            )
        return name

    async def add(self, guild_id: int, uploader_id: int, name: str, upload: bytes) -> Sound:
        name = self.check_can_add(guild_id, name, len(upload))
        sound_id = secrets.token_hex(16)
        path = self._path(guild_id, sound_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        async with self._conversions:
            with tempfile.TemporaryDirectory(dir=self._tmp_dir) as tmp:
                # A fixed name without extension: FFmpeg must recognise the file by its
                # content, not by whatever name the uploader gave it.
                source = Path(tmp) / "upload"
                await asyncio.to_thread(source.write_bytes, upload)
                duration_ms = await audio.convert_upload(
                    source,
                    path,
                    ffmpeg=self._settings.ffmpeg,
                    ffprobe=self._settings.ffprobe,
                    max_duration=self._settings.max_duration,
                    loudness_lufs=self._settings.loudness_lufs,
                    true_peak_dbtp=self._settings.true_peak_dbtp,
                )

        sound = Sound(sound_id, guild_id, name, uploader_id, duration_ms, datetime.now(UTC))
        try:
            self._store.add(sound, max_per_guild=self._settings.max_sounds)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self._events.publish(guild_id, Topic.SOUNDS)
        return sound

    def rename(self, sound: Sound, new_name: str) -> Sound:
        # Only the row changes: files are named after the sound's ID, never its name.
        name = normalise_name(new_name)
        if name == sound.name:
            raise UserError(f"It's already called `{name}`.")
        if not self._store.rename(sound.id, name):
            raise SoundNotFound("There's no sound by that name, see /list.")
        self._events.publish(sound.guild_id, Topic.SOUNDS)
        return replace(sound, name=name)

    def remove(self, sound: Sound) -> None:
        # Row first: if deleting the file fails, what's left is an unreferenced file, never
        # a listed sound that can't play.
        self._store.remove(sound.id)
        self._events.publish(sound.guild_id, Topic.SOUNDS)
        self._path(sound.guild_id, sound.id).unlink(missing_ok=True)

    def clean_up(self) -> None:
        """Delete what interrupted uploads and removals left behind. Run before any upload.

        Assumes this process is the only one writing to the data directory: another
        process's upload in progress would look abandoned.
        """
        if self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir)
        listed = {self._path(sound.guild_id, sound.id): sound for sound in self._store.all_sounds()}
        # Only names this module writes: anything else in the directory isn't ours to delete.
        stale = [
            path
            for path in self._sounds_dir.glob("*/*")
            if path.name.endswith((".ogg", ".ogg.part")) and path not in listed
        ]
        for path in stale:
            path.unlink()
        if stale:
            _log.info("Deleted %d sound file(s) no longer in the database", len(stale))
        for path, sound in listed.items():
            if not path.exists():
                # Deleting the row would hide the problem: this is a backup that saved
                # minie.db without sounds/, or the other way round.
                _log.warning(
                    "Sound %r of guild %s has no file at %s", sound.name, sound.guild_id, path
                )

    async def frames(self, sound: Sound) -> list[bytes]:
        return await asyncio.to_thread(read_opus_frames, self._path(sound.guild_id, sound.id))

    def _path(self, guild_id: int, sound_id: str) -> Path:
        return self._sounds_dir / str(guild_id) / f"{sound_id}.ogg"
