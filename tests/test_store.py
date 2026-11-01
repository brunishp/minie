from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minie.store import GuildFullError, Sound, SoundExistsError, SoundStore


def sound(name: str, guild_id: int = 1) -> Sound:
    created = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    return Sound(f"id-{guild_id}-{name}", guild_id, name, 42, 1500, created)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SoundStore]:
    store = SoundStore(tmp_path / "minie.db")
    yield store
    store.close()


def test_round_trip(store: SoundStore) -> None:
    airhorn = sound("airhorn")
    store.add(airhorn, max_per_guild=10)
    assert store.get(1, "airhorn") == airhorn
    assert store.get(2, "airhorn") is None


def test_lists_a_guild_by_name(store: SoundStore) -> None:
    for name in ("zeta", "alpha"):
        store.add(sound(name), max_per_guild=10)
    store.add(sound("other", guild_id=2), max_per_guild=10)
    assert [s.name for s in store.sounds(1)] == ["alpha", "zeta"]
    assert store.count(1) == 2


def test_search_ranks_prefix_matches_first(store: SoundStore) -> None:
    for name in ("bad_horn", "horn", "airhorn", "drum"):
        store.add(sound(name), max_per_guild=10)
    assert [s.name for s in store.search(1, "horn")] == ["horn", "airhorn", "bad_horn"]
    assert [s.name for s in store.search(1, "_")] == ["bad_horn"]  # literal, not a wildcard
    assert len(store.search(1, "")) == 4
    assert len(store.search(1, "", limit=2)) == 2


def test_names_are_unique_within_a_guild(store: SoundStore) -> None:
    store.add(sound("horn"), max_per_guild=10)
    duplicate = Sound("another-id", 1, "horn", 7, 900, datetime.now(UTC))
    with pytest.raises(SoundExistsError):
        store.add(duplicate, max_per_guild=10)
    store.add(sound("horn", guild_id=2), max_per_guild=10)


def test_enforces_the_per_guild_limit(store: SoundStore) -> None:
    store.add(sound("a"), max_per_guild=2)
    store.add(sound("b"), max_per_guild=2)
    with pytest.raises(GuildFullError):
        store.add(sound("c"), max_per_guild=2)
    store.add(sound("c", guild_id=2), max_per_guild=2)


def test_rename_keeps_names_unique(store: SoundStore) -> None:
    store.add(sound("a"), max_per_guild=10)
    store.add(sound("b"), max_per_guild=10)
    store.add(sound("c", guild_id=2), max_per_guild=10)
    assert store.rename("id-1-a", "c") is True  # "c" is only taken in another guild
    assert [s.name for s in store.sounds(1)] == ["b", "c"]
    with pytest.raises(SoundExistsError):
        store.rename("id-1-b", "c")
    assert store.rename("no-such-id", "d") is False


def test_remove(store: SoundStore) -> None:
    store.add(sound("a"), max_per_guild=10)
    store.remove("id-1-a")
    assert store.get(1, "a") is None


def test_all_sounds_spans_guilds(store: SoundStore) -> None:
    store.add(sound("b", guild_id=2), max_per_guild=10)
    store.add(sound("a"), max_per_guild=10)
    assert [(s.guild_id, s.name) for s in store.all_sounds()] == [(1, "a"), (2, "b")]


def test_schema_is_created_once(tmp_path: Path) -> None:
    path = tmp_path / "minie.db"
    first = SoundStore(path)
    first.add(sound("a"), max_per_guild=10)
    first.close()
    reopened = SoundStore(path)
    assert reopened.schema_version() == 1
    assert reopened.count(1) == 1
    reopened.close()


def test_opening_is_deferred_until_first_use(tmp_path: Path) -> None:
    SoundStore(tmp_path / "sub" / "minie.db").close()
    assert not (tmp_path / "sub").exists()
