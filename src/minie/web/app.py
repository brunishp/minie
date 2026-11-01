"""The aiohttp application: request checks, the sign-in pages, static files and startup."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from aiohttp.typedefs import Handler

from minie.config import Settings
from minie.errors import UserError
from minie.web import api
from minie.web.auth import (
    SESSION_COOKIE,
    SESSION_LIFETIME,
    STATE_COOKIE,
    STATE_LIFETIME,
    DiscordOAuth,
    OAuthError,
    Sessions,
    new_state,
    same_state,
    set_cookie,
)
from minie.web.core import OAUTH, SERVICES, SESSIONS, SETTINGS, STREAMS, ApiError, Services

_log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS = {
    # No inline script anywhere; blob: is for the silent clip that arms the media keys.
    "Content-Security-Policy": (
        "default-src 'self'; media-src 'self' blob:; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def create_app(
    services: Services,
    settings: Settings,
    *,
    discord_api: str = "https://discord.com/api/v10",
    discord_authorize: str = "https://discord.com/oauth2/authorize",
) -> web.Application:
    assert settings.oauth_client_secret is not None, "checked by preflight"
    app = web.Application(middlewares=[_check_host, _when_ready, _check_origin, _errors])
    app[SERVICES] = services
    app[SETTINGS] = settings
    app[SESSIONS] = Sessions()
    app[STREAMS] = set()

    async def http_client(app: web.Application) -> AsyncIterator[None]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            app[OAUTH] = DiscordOAuth(
                http,
                client_secret=settings.oauth_client_secret or "",
                redirect_uri=f"{settings.web_url}/auth/callback",
                api=discord_api,
                authorize=discord_authorize,
            )
            yield

    app.cleanup_ctx.append(http_client)
    app.on_shutdown.append(_close_streams)
    app.on_response_prepare.append(_add_security_headers)
    app.router.add_get("/", _index)
    app.router.add_get("/auth/login", _login)
    app.router.add_get("/auth/callback", _callback)
    app.router.add_post("/auth/logout", _logout)
    app.router.add_routes(api.routes)
    app.router.add_static("/static", STATIC)
    return app


async def start(services: Services, settings: Settings) -> web.AppRunner:
    runner = web.AppRunner(create_app(services, settings), **runner_options())
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    _log.info("Web UI at %s", settings.web_url)
    return runner


def runner_options() -> dict[str, Any]:
    return {
        "access_log_class": _AccessLogger,
        "access_log": logging.getLogger("minie.web.access"),
        # Long-lived event streams are cancelled on shutdown; nothing else runs long.
        "shutdown_timeout": 5,
    }


async def _close_streams(app: web.Application) -> None:
    for task in list(app[STREAMS]):
        task.cancel()


class _AccessLogger(AbstractAccessLogger):
    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        # The path only, never the query string: the sign-in callback carries the OAuth code.
        self.logger.info(
            "%s %s %s %.0f ms", request.method, request.path, response.status, time * 1000
        )


async def _add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)


@web.middleware
async def _check_host(request: web.Request, handler: Handler) -> web.StreamResponse:
    # Answering only to our own host name also defeats DNS rebinding, where a hostile site
    # points its name at 127.0.0.1 to reach a local server.
    public = urlsplit(request.app[SETTINGS].web_url)
    if request.host.lower() != public.netloc.lower():
        if request.method == "GET":
            raise web.HTTPFound(f"{request.app[SETTINGS].web_url}{request.rel_url.path_qs}")
        raise web.HTTPMisdirectedRequest()
    return await handler(request)


@web.middleware
async def _when_ready(request: web.Request, handler: Handler) -> web.StreamResponse:
    if request.path.startswith(("/api/", "/auth/")) and not request.app[SERVICES].ready():
        return web.json_response({"error": "Starting up, try again in a moment."}, status=503)
    return await handler(request)


@web.middleware
async def _check_origin(request: web.Request, handler: Handler) -> web.StreamResponse:
    # Cross-site requests can't send our cookie with a JSON body or a custom header without a
    # CORS preflight, which this server never answers. The Origin check adds a second wall.
    if request.method in UNSAFE_METHODS:
        same_origin = request.headers.get("Origin") == request.app[SETTINGS].web_url
        expected_body = request.content_type == "application/json" or "X-Minie" in request.headers
        if not (same_origin and expected_body):
            return web.json_response({"error": "Cross-site request refused."}, status=403)
    return await handler(request)


@web.middleware
async def _errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as error:
        return web.json_response({"error": error.message}, status=error.status)
    except UserError as error:
        return await api.user_error_response(request, error)
    except web.HTTPException:
        raise
    except Exception:
        _log.exception("%s %s failed", request.method, request.path)
        return web.json_response(
            {"error": "Something went wrong, the error has been logged."}, status=500
        )


async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def _login(request: web.Request) -> web.Response:
    client_id = request.app[SERVICES].application_id()
    if client_id is None:
        return web.json_response({"error": "Starting up, try again in a moment."}, status=503)
    state = new_state()
    response = web.Response(status=302)
    response.headers["Location"] = request.app[OAUTH].authorize_url(client_id, state)
    set_cookie(response, STATE_COOKIE, state, max_age=STATE_LIFETIME, secure=_secure(request))
    return response


async def _callback(request: web.Request) -> web.Response:
    if not same_state(request.cookies.get(STATE_COOKIE), request.query.get("state")):
        return _failed_sign_in("The sign-in link expired or came from elsewhere.")
    code = request.query.get("code")
    client_id = request.app[SERVICES].application_id()
    if not code or client_id is None:  # cancelled on Discord's side, or an error from it
        return _failed_sign_in("Discord didn't confirm the sign-in.")
    try:
        user = await request.app[OAUTH].sign_in(client_id, code)
    except (OAuthError, aiohttp.ClientError, KeyError, ValueError) as exc:
        _log.warning("Sign-in failed: %s", exc)
        return _failed_sign_in("Discord didn't confirm the sign-in.")
    token = request.app[SESSIONS].create(user.id, user.name, user.guild_ids)
    _log.info("User %s signed in to the web UI", user.id)
    response = web.Response(status=302)
    response.headers["Location"] = "/"
    set_cookie(
        response, SESSION_COOKIE, token, max_age=int(SESSION_LIFETIME), secure=_secure(request)
    )
    response.del_cookie(STATE_COOKIE, path="/")
    return response


async def _logout(request: web.Request) -> web.Response:
    request.app[SESSIONS].delete(request.cookies.get(SESSION_COOKIE))
    response = web.Response(status=204)
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


def _failed_sign_in(reason: str) -> web.Response:
    # Fixed text only: nothing from the query string is echoed back.
    return web.Response(
        status=400,
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8><title>minie</title>"
            f"<p>Sign-in failed. {reason}</p><p><a href=/auth/login>Try again</a></p>"
        ),
    )


def _secure(request: web.Request) -> bool:
    return request.app[SETTINGS].web_url.startswith("https://")
