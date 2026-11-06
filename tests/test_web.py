import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from aiohttp import ClientResponse
from web_harness import (
    ALICE,
    BOB,
    ELSEWHERE,
    GUILD,
    HERE,
    HORN,
    UPLOAD_LIMIT,
    Harness,
    running,
)

from minie.audio import run_ffmpeg
from minie.store import SoundStore
from minie.web.auth import SESSION_LIFETIME, Sessions


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SoundStore]:
    store = SoundStore(tmp_path / "minie.db")
    yield store
    store.close()


@pytest.fixture
async def web(tmp_path: Path, store: SoundStore) -> AsyncIterator[Harness]:
    async with running(Harness(tmp_path, store)) as harness:
        yield harness


# Sign-in


async def test_sign_in_then_the_token_is_revoked(web: Harness) -> None:
    await web.sign_in(ALICE, "Alice")
    assert web.discord.revoked == [f"access-code-{ALICE}"]
    me = await (await web.client.get("/api/me")).json()
    assert me == {
        "user": {"id": str(ALICE), "name": "Alice"},
        "guilds": [{"id": "1", "name": "Test server"}],  # only servers shared with the bot
    }


async def test_the_api_needs_a_session(web: Harness) -> None:
    response = await web.client.get("/api/me")
    assert response.status == 401
    assert await response.json() == {"error": "Sign in first."}


async def test_a_foreign_state_is_refused(web: Harness) -> None:
    await web.client.get("/auth/login", allow_redirects=False)
    response = await web.client.get(
        "/auth/callback", params={"code": "code-10", "state": "forged"}, allow_redirects=False
    )
    assert response.status == 400
    assert "forged" not in await response.text()  # nothing from the query is echoed
    assert (await web.client.get("/api/me")).status == 401


async def test_a_refused_code_signs_nobody_in(web: Harness) -> None:
    login = await web.client.get("/auth/login", allow_redirects=False)
    state = login.cookies["minie_oauth_state"].value
    response = await web.client.get(
        "/auth/callback", params={"code": "unknown", "state": state}, allow_redirects=False
    )
    assert response.status == 400
    assert (await web.client.get("/api/me")).status == 401


async def test_sign_out(web: Harness) -> None:
    await web.sign_in()
    assert (await web.post("/auth/logout")).status == 204
    assert (await web.client.get("/api/me")).status == 401


def test_sessions_expire() -> None:
    now = [1000.0]
    sessions = Sessions(clock=lambda: now[0])
    token = sessions.create(ALICE, "Alice", frozenset({1}))
    assert sessions.get(token) is not None
    assert sessions.get(token + "x") is None
    assert sessions.get(None) is None
    now[0] += SESSION_LIFETIME
    assert sessions.get(token) is None


# Request checks


async def test_cross_site_writes_are_refused(web: Harness) -> None:
    await web.sign_in()
    no_origin = await web.client.post("/auth/logout", json={})
    foreign = await web.client.post(
        "/auth/logout", json={}, headers={"Origin": "https://evil.example"}
    )
    form = await web.client.post("/auth/logout", data={"a": "b"}, headers={"Origin": web.url})
    assert [r.status for r in (no_origin, foreign, form)] == [403, 403, 403]
    assert (await web.client.get("/api/me")).status == 200  # still signed in


async def test_other_host_names_are_sent_to_the_public_url(web: Harness) -> None:
    response = await web.client.get(
        "/api/me?x=1", headers={"Host": "rebound.example"}, allow_redirects=False
    )
    assert response.status == 302
    assert response.headers["Location"] == f"{web.url}/api/me?x=1"
    post = await web.client.post(
        "/auth/logout", json={}, headers={"Host": "rebound.example", "Origin": web.url}
    )
    assert post.status == 421


async def test_answers_503_until_discord_is_ready(web: Harness) -> None:
    web.ready = False
    response = await web.client.get("/api/me")
    assert response.status == 503
    assert (await web.client.get("/auth/login", allow_redirects=False)).status == 503
    assert (await web.client.get("/")).status == 200  # the page itself still loads


async def test_security_headers(web: Harness) -> None:
    for path in ("/", "/static/app.js", "/api/me"):
        headers = (await web.client.get(path)).headers
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"


async def test_the_access_log_leaves_out_query_strings(
    web: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="minie.web.access"):
        await web.client.get(
            "/auth/callback", params={"code": "secret-code", "state": "s"}, allow_redirects=False
        )
        for _ in range(100):  # the server logs once the response is sent
            if any(r.name == "minie.web.access" for r in caplog.records):
                break
            await asyncio.sleep(0.01)
    lines = [r.getMessage() for r in caplog.records if r.name == "minie.web.access"]
    assert lines and lines[0].startswith("GET /auth/callback 400")
    assert "secret-code" not in caplog.text


# Bot control


async def test_list_sounds_with_who_may_manage_them(web: Harness) -> None:
    web.member(BOB)
    web.sound("horn", uploader=ALICE)
    web.sound("drum", uploader=BOB)
    await web.sign_in(BOB, "Bob")
    body = await (await web.client.get(f"/api/guilds/{GUILD}/sounds")).json()
    assert [(s["name"], s["can_manage"]) for s in body["sounds"]] == [
        ("drum", True),
        ("horn", False),
    ]
    assert body["sounds"][0]["uploader_id"] == str(BOB)


async def test_non_members_are_refused(web: Harness) -> None:
    await web.sign_in(BOB, "Bob")  # signed in, but not in the server (any more)
    response = await web.client.get(f"/api/guilds/{GUILD}/sounds")
    assert response.status == 403
    assert (await web.client.get("/api/guilds/not-a-number/sounds")).status == 404


async def test_play_joins_the_members_channel(web: Harness) -> None:
    web.member(ALICE, HERE)
    web.sound("horn")
    await web.sign_in()
    response = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert await response.json() == {"position": 0}
    assert web.voice.joins == [HERE]
    assert web.voice.started == [HORN]


async def test_play_errors_name_channels_the_member_may_see(web: Harness) -> None:
    web.member(ALICE, HERE)
    web.sound("horn")
    web.presence.names = {ELSEWHERE: "gaming"}
    await web.sign_in()
    web.voice.channel = ELSEWHERE
    await web.playback.play(GUILD, BOB, ELSEWHERE, HORN)  # busy over there

    busy = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert busy.status == 409
    assert (await busy.json())["error"].startswith("I'm busy in #gaming.")

    web.presence.hidden_from.add((ALICE, ELSEWHERE))
    hidden = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert (await hidden.json())["error"].startswith("I'm busy in a channel you can't see.")


async def test_play_validates_its_input(web: Harness) -> None:
    web.member(ALICE, HERE)
    await web.sign_in()
    missing = await web.post(f"/api/guilds/{GUILD}/play", {"name": 3})
    assert missing.status == 400
    unknown = await web.post(f"/api/guilds/{GUILD}/play", {"name": "nothing"})
    assert unknown.status == 404
    web.member(ALICE, None)
    not_in_voice = await web.post(f"/api/guilds/{GUILD}/play", {"name": "nothing"})
    assert (await not_in_voice.json())["error"] == "Join a voice channel first."


async def test_status_stop_and_leave(web: Harness) -> None:
    web.member(ALICE, HERE)
    web.sound("horn")
    web.presence.names = {HERE: "lounge"}
    await web.sign_in()
    status = await (await web.client.get(f"/api/guilds/{GUILD}/status")).json()
    assert status == {
        "channel": None,
        "playing": False,
        "queued": 0,
        "with_you": False,
        "can_leave": True,
    }

    await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    status = await (await web.client.get(f"/api/guilds/{GUILD}/status")).json()
    assert status["channel"] == {"id": str(HERE), "name": "lounge"}
    assert (status["playing"], status["with_you"]) == (True, True)

    assert await (await web.post(f"/api/guilds/{GUILD}/stop")).json() == {"stopped": True}
    assert await (await web.post(f"/api/guilds/{GUILD}/leave")).json() == {"left": True}
    assert web.voice.channel is None


async def test_leave_needs_the_channel_or_manage_server(web: Harness) -> None:
    web.member(ALICE, ELSEWHERE)
    web.presence.names = {HERE: "lounge"}
    web.voice.channel = HERE
    await web.sign_in()
    status = await (await web.client.get(f"/api/guilds/{GUILD}/status")).json()
    assert status["can_leave"] is False
    refused = await web.post(f"/api/guilds/{GUILD}/leave")
    assert refused.status == 403
    assert (await refused.json())["error"].startswith("Join #lounge first")
    assert web.voice.channel == HERE


async def test_errors_call_the_members_own_channel_theirs(web: Harness) -> None:
    web.member(ALICE, HERE)
    web.sound("horn")
    web.presence.hidden_from.add((ALICE, HERE))  # what a placeholder overwrite could suggest
    await web.sign_in()
    web.presence.unjoinable[HERE] = f"I'm missing permissions in <#{HERE}>: View Channel."
    missing = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert (await missing.json())["error"] == (
        "I'm missing permissions in your voice channel: View Channel."
    )
    web.presence.unjoinable[HERE] = f"<#{HERE}> is full."
    full = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert (await full.json())["error"] == "Your voice channel is full."


async def test_events_follow_changes_from_any_source(web: Harness) -> None:
    web.member(ALICE, HERE)
    web.sound("horn")
    await web.sign_in()
    stream = await web.client.get(f"/api/guilds/{GUILD}/events")
    assert stream.headers["Content-Type"] == "text/event-stream"
    assert await stream.content.readline() == b"retry: 3000\n"
    await stream.content.readline()

    # Through Soundboard, as a slash command would.
    web.soundboard.rename(web.member(ALICE), "horn", "drum")
    assert await _next_event(stream) == "sounds"
    await web.post(f"/api/guilds/{GUILD}/play", {"name": "drum"})
    assert await _next_event(stream) == "status"
    stream.close()


async def _next_event(stream: ClientResponse) -> str:
    async with asyncio.timeout(2):
        while True:
            line = (await stream.content.readline()).decode().strip()
            if line.startswith("event: "):
                return line.removeprefix("event: ")


# Managing sounds


@pytest.fixture
def beep(tmp_path: Path) -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    path = tmp_path / "beep.wav"
    run_ffmpeg(
        "ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-ar", "8000", str(path)
    )
    return path.read_bytes()


async def test_upload(web: Harness, beep: bytes) -> None:
    web.member(ALICE)
    await web.sign_in()
    response = await web.upload("Beep", beep)
    assert response.status == 200, await response.text()
    assert (await response.json())["name"] == "beep"
    assert [sound.name for sound in web.library.sounds(GUILD)] == ["beep"]
    duplicate = await web.upload("beep", beep)
    assert duplicate.status == 409


async def test_upload_refusals(web: Harness) -> None:
    web.member(ALICE)
    await web.sign_in()
    bad_name = await web.upload("../escape", b"x")
    assert bad_name.status == 400
    assert "1 to 32 characters" in (await bad_name.json())["error"]
    too_big = await web.upload("big", b"x" * (UPLOAD_LIMIT + 1))
    assert too_big.status == 413
    announced_too_big = await web.upload("huge", b"x" * (UPLOAD_LIMIT + 100_000))
    assert announced_too_big.status == 413
    assert (await web.upload("beep", b"x", csrf_header=False)).status == 403
    not_multipart = await web.post(f"/api/guilds/{GUILD}/sounds", {"name": "x"})
    assert not_multipart.status == 400
    assert web.library.sounds(GUILD) == []


async def test_rename_and_remove(web: Harness) -> None:
    web.member(ALICE)
    web.member(BOB)
    web.sound("horn", uploader=ALICE)
    await web.sign_in(BOB, "Bob")
    refused = await web.client.patch(
        f"/api/guilds/{GUILD}/sounds/horn", json={"name": "mine"}, headers={"Origin": web.url}
    )
    assert refused.status == 403

    await web.sign_in(ALICE, "Alice")
    renamed = await web.client.patch(
        f"/api/guilds/{GUILD}/sounds/horn", json={"name": "Air_Horn"}, headers={"Origin": web.url}
    )
    assert await renamed.json() == {"name": "air_horn"}
    gone = await web.client.delete(
        f"/api/guilds/{GUILD}/sounds/horn", json={}, headers={"Origin": web.url}
    )
    assert gone.status == 404
    removed = await web.client.delete(
        f"/api/guilds/{GUILD}/sounds/air_horn", json={}, headers={"Origin": web.url}
    )
    assert removed.status == 204
    assert web.library.sounds(GUILD) == []


async def test_commands_restricted_in_discord_are_refused(web: Harness) -> None:
    web.member(ALICE)
    web.sound("horn")
    await web.sign_in()
    web.presence.restricted = {"add"}
    refused = await web.upload("beep", b"x")
    assert refused.status == 403
    assert "restricts /add" in (await refused.json())["error"]
    played = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert played.status == 200

    web.presence.restricted = {"*"}  # the whole application
    blocked = await web.post(f"/api/guilds/{GUILD}/play", {"name": "horn"})
    assert blocked.status == 403
    assert "restricts /play" in (await blocked.json())["error"]
