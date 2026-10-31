import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from minie.audio import AudioRejected, run_ffmpeg
from minie.config import Settings
from minie.errors import UserError
from minie.library import SoundLibrary, normalise_name
from minie.store import GuildFullError, SoundExistsError, SoundStore

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def library(tmp_path: Path) -> Iterator[SoundLibrary]:
    store = SoundStore(tmp_path / "minie.db")
    yield SoundLibrary(store, Settings(data_dir=tmp_path, max_sounds=2, max_upload_bytes=2**20))
    store.close()


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
