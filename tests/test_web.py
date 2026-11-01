import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from web_harness import ALICE, Harness, running

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
