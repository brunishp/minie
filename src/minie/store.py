"""Sound metadata in SQLite. The audio lives in files, managed by `minie.library`.

Queries run directly on the event loop thread. On a local file holding a few hundred rows
they take microseconds, less than handing them to a worker thread would cost.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path

from minie.errors import UserError

# Entry N upgrades the schema from version N to N + 1, tracked in PRAGMA user_version.
_MIGRATIONS = (
    """
    CREATE TABLE sounds (
        id          TEXT PRIMARY KEY,  -- random hex, also the audio file's name
        guild_id    INTEGER NOT NULL,
        name        TEXT NOT NULL,     -- normalised to lowercase by the library
        uploader_id INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,
        created_at  TEXT NOT NULL,     -- ISO 8601, UTC
        UNIQUE (guild_id, name)
    ) STRICT;
    """,
)

_COLUMNS = "id, guild_id, name, uploader_id, duration_ms, created_at"


@dataclass(frozen=True)
class Sound:
    id: str
    guild_id: int
    name: str
    uploader_id: int
    duration_ms: int
    created_at: datetime


class SoundExistsError(UserError):
    pass


class GuildFullError(UserError):
    pass


class SoundStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @cached_property
    def _db(self) -> sqlite3.Connection:
        # Opened on first use, so that building the bot (for `minie sync`, or in tests)
        # creates no files.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path)
        # WAL: a crash never corrupts the database, and readers (a future web UI process)
        # don't block the writer. With WAL, synchronous=NORMAL can lose at most the last
        # commits on power loss, never consistency.
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 5000")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            db.executescript(f"BEGIN; {script} PRAGMA user_version = {number}; COMMIT;")
        return db

    def schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    def get(self, guild_id: int, name: str) -> Sound | None:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM sounds WHERE guild_id = ? AND name = ?", (guild_id, name)
        ).fetchone()
        return _sound(row) if row else None

    def sounds(self, guild_id: int) -> list[Sound]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM sounds WHERE guild_id = ? ORDER BY name", (guild_id,)
        )
        return [_sound(row) for row in rows]

    def search(self, guild_id: int, text: str, *, limit: int = 25) -> list[Sound]:
        """Sounds whose name contains `text`, those starting with it first."""
        # instr() rather than LIKE: `_`, which names may contain, is a LIKE wildcard.
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM sounds WHERE guild_id = ? AND instr(name, ?) > 0 "
            "ORDER BY instr(name, ?) != 1, name LIMIT ?",
            (guild_id, text, text, limit),
        )
        return [_sound(row) for row in rows]

    def count(self, guild_id: int) -> int:
        return self._db.execute(
            "SELECT count(*) FROM sounds WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]

    def add(self, sound: Sound, *, max_per_guild: int) -> None:
        """Insert `sound`, atomically enforcing unique names and the per-guild limit.

        Two uploads racing for the same name, or for the last free slot, can both pass
        the checks made before converting; only one insert wins here.
        """
        try:
            with self._db:
                cursor = self._db.execute(
                    f"INSERT INTO sounds ({_COLUMNS}) SELECT ?, ?, ?, ?, ?, ? "
                    "WHERE (SELECT count(*) FROM sounds WHERE guild_id = ?) < ?",
                    (
                        sound.id,
                        sound.guild_id,
                        sound.name,
                        sound.uploader_id,
                        sound.duration_ms,
                        sound.created_at.isoformat(),
                        sound.guild_id,
                        max_per_guild,
                    ),
                )
        except sqlite3.IntegrityError:
            raise SoundExistsError(f"There's already a sound called `{sound.name}`.") from None
        if cursor.rowcount == 0:
            raise GuildFullError(f"This server already has {max_per_guild} sounds, the maximum.")

    def remove(self, sound_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM sounds WHERE id = ?", (sound_id,))

    def close(self) -> None:
        if "_db" in self.__dict__:
            self._db.close()
            del self._db


def _sound(row: tuple[str, int, str, int, int, str]) -> Sound:
    id_, guild_id, name, uploader_id, duration_ms, created_at = row
    return Sound(id_, guild_id, name, uploader_id, duration_ms, datetime.fromisoformat(created_at))
