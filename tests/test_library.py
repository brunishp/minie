import logging
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minie.audio import AudioRejected, run_ffmpeg
from minie.config import Settings
from minie.errors import UserError
from minie.library import SoundLibrary, normalise_name
from minie.store import GuildFullError, Sound, SoundExistsError, SoundStore

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SoundStore]:
    store = SoundStore(tmp_path / "minie.db")
    yield store
    store.close()


@pytest.fixture
def library(tmp_path: Path, store: SoundStore) -> SoundLibrary:
    return SoundLibrary(store, Settings(data_dir=tmp_path, max_sounds=2, max_upload_bytes=2**20))


def listed(store: SoundStore, name: str, guild_id: int = 1) -> Sound:
    """A sound in the database only, for tests that don't need FFmpeg."""
    sound = Sound(f"{name}-id", guild_id, name, 42, 1000, datetime(2026, 11, 1, tzinfo=UTC))
    store.add(sound, max_per_guild=10)
    return sound


@pytest.fixture
def beep(tmp_path: Path) -> bytes:
    path = tmp_path / "beep.wav"
    run_ffmpeg("ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(path))
    return path.read_bytes()


def files_under(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file()] if path.exists() else []


@pytest.mark.parametrize(("raw", "name"), [("AirHorn", "airhorn"), (" bad_horn-2 ", "bad_horn-2")])
def test_names_are_normalised(raw: str, name: str) -> None:
    assert normalise_name(raw) == name


@pytest.mark.parametrize("raw", ["", "two words", "émoji", "x" * 33, "dot.ogg", "../up"])
def test_rejects_invalid_names(raw: str) -> None:
    with pytest.raises(UserError, match="1 to 32 characters"):
        normalise_name(raw)


def test_rejects_oversized_uploads_before_downloading(library: SoundLibrary) -> None:
    with pytest.raises(UserError, match="limit is 1 MiB"):
        library.check_can_add(1, "big", 2 * 2**20)


def test_enforces_name_rules_itself(library: SoundLibrary) -> None:
    # Not only the slash command: the web UI will call the library directly.
    assert library.check_can_add(1, " Air_Horn ", 100) == "air_horn"
    with pytest.raises(UserError, match="1 to 32 characters"):
        library.check_can_add(1, "../escape", 100)


@needs_ffmpeg
async def test_add_find_play_remove(tmp_path: Path, library: SoundLibrary, beep: bytes) -> None:
    sound = await library.add(1, 42, "Beep", beep)
    assert sound.name == "beep"
    assert library.find(1, "BEEP") == sound
    assert 50 <= len(await library.frames(sound)) <= 52
    library.remove(sound)
    assert library.sounds(1) == []
    assert files_under(tmp_path / "sounds") == []
    assert files_under(tmp_path / "tmp") == []


@needs_ffmpeg
async def test_rejects_duplicates_and_full_libraries(library: SoundLibrary, beep: bytes) -> None:
    await library.add(1, 42, "a", beep)
    with pytest.raises(SoundExistsError):
        await library.add(1, 42, "a", beep)
    await library.add(1, 42, "b", beep)
    with pytest.raises(GuildFullError):
        await library.add(1, 42, "c", beep)


@needs_ffmpeg
async def test_a_rejected_upload_leaves_nothing_behind(
    tmp_path: Path, library: SoundLibrary
) -> None:
    with pytest.raises(AudioRejected):
        await library.add(1, 42, "junk", b"definitely not audio")
    assert library.sounds(1) == []
    assert files_under(tmp_path / "sounds") == []
    assert files_under(tmp_path / "tmp") == []


def test_rename(library: SoundLibrary, store: SoundStore) -> None:
    horn = listed(store, "horn")
    renamed = library.rename(horn, " Air_Horn ")
    assert renamed.name == "air_horn"
    assert renamed.id == horn.id  # same file: only the name changes
    assert library.find(1, "air_horn") == renamed
    with pytest.raises(UserError, match="no sound by that name"):
        library.find(1, "horn")


def test_rename_refusals(library: SoundLibrary, store: SoundStore) -> None:
    horn = listed(store, "horn")
    listed(store, "drum")
    with pytest.raises(UserError, match="already called"):
        library.rename(horn, "HORN")
    with pytest.raises(UserError, match="1 to 32 characters"):
        library.rename(horn, "../up")
    with pytest.raises(SoundExistsError):
        library.rename(horn, "drum")
    library.remove(horn)
    with pytest.raises(UserError, match="no sound by that name"):
        library.rename(horn, "gone")  # removed while the command ran


def test_clean_up(
    tmp_path: Path, library: SoundLibrary, store: SoundStore, caplog: pytest.LogCaptureFixture
) -> None:
    kept = tmp_path / "sounds" / "1" / "horn-id.ogg"
    lost = listed(store, "lost", guild_id=2)  # listed, but its file is gone
    listed(store, "horn")
    leftovers = [
        tmp_path / "sounds" / "1" / "removed-id.ogg",  # the row was deleted, the file wasn't
        tmp_path / "sounds" / "1" / "crashed-id.ogg.part",  # an encode cut short
        tmp_path / "tmp" / "tmpabc" / "upload",  # an upload cut short
    ]
    unknown = tmp_path / "sounds" / "README.txt"
    for path in (kept, unknown, *leftovers):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    with caplog.at_level(logging.INFO, logger="minie.library"):
        library.clean_up()

    assert sorted(files_under(tmp_path / "sounds")) == sorted([kept, unknown])
    assert not (tmp_path / "tmp").exists()
    assert "Deleted 2 sound file(s)" in caplog.text
    assert "'lost' of guild 2 has no file" in caplog.text
    assert store.get(2, lost.name) == lost  # reported, not deleted


def test_clean_up_with_nothing_there(library: SoundLibrary) -> None:
    library.clean_up()
