"""Mount the MCP server into the wbot FastAPI app at ``/mcp`` (streamable HTTP).

The MCP tools call the REST API of the *same* app in-process (``httpx.ASGITransport``), so
there is no loopback network traffic and the web UI and the AI share one Machine.

* The server (and the heavy ``mcp`` import) is created lazily on the first request.
* Access: if the environment variable ``WBOT_MCP_TOKEN`` is set, every request needs
  ``Authorization: Bearer <token>``. Without a token only loopback hosts / the configured
  ``api.host`` are accepted (DNS-rebinding protection: Host and Origin are checked).
* ``mcp.enabled: false`` in the config makes ``/mcp`` answer 503 (read live).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

log = logging.getLogger(__name__)
TOKEN_ENV = "WBOT_MCP_TOKEN"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _hostname(value: str) -> str:
    """Host part of a Host header or Origin URL, without port (IPv6 brackets kept for matching)."""
    v = value if "//" in value else f"//{value}"
    h = urlsplit(v).hostname or ""
    return h.lower()


class MountedMcp:
    """ASGI endpoint for ``/mcp``; lazily builds the MCP server, runs its session manager."""

    def __init__(self, app, machine) -> None:
        self.app, self.machine = app, machine
        self._asgi = None
        self._api = None
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None

    # ---------------------------------------------------------------- access control
    def _guard(self, scope: Scope) -> JSONResponse | None:
        cfg = self.machine.cfg
        if not cfg.mcp.enabled:
            return JSONResponse({"error": "MCP control is disabled in the configuration (mcp.enabled).",
                                 "code": "mcp_disabled"}, status_code=503)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token = os.environ.get(TOKEN_ENV)
        if token:
            given = headers.get("authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
                return JSONResponse({"error": "Invalid or missing MCP token.", "code": "unauthorized"},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return None
        allowed = LOOPBACK | {cfg.api.host.lower()}
        host = _hostname(headers.get("host", ""))
        origin = headers.get("origin")
        if host not in allowed or (origin and _hostname(origin) not in allowed):
            return JSONResponse({"error": "Host/Origin not allowed (DNS-rebinding protection). To allow access "
                                          f"from other addresses, set the environment variable {TOKEN_ENV}.",
                                 "code": "forbidden_host"}, status_code=421 if host not in allowed else 403)
        return None

    # ---------------------------------------------------------------- lifecycle
    async def _ensure(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._asgi is not None:
                return self._asgi
            from mcp.server.transport_security import TransportSecuritySettings

            from .mcp_server import Api, build_server
            transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 0))
            client = httpx.AsyncClient(transport=transport, base_url="http://wbot.internal",
                                       timeout=httpx.Timeout(300.0))
            self._api = Api(client, "in-process")
            server = build_server(self._api)
            sub = server.streamable_http_app(
                streamable_http_path="/mcp",
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))  # see _guard
            manager = server.session_manager
            ready, self._stop = asyncio.Event(), asyncio.Event()
            failure: list[BaseException] = []

            async def runner() -> None:
                try:
                    async with manager.run():
                        ready.set()
                        await self._stop.wait()
                except BaseException as exc:  # noqa: BLE001
                    failure.append(exc)
                    raise
                finally:
                    ready.set()

            self._task = asyncio.create_task(runner(), name="mcp-session-manager")
            await ready.wait()
            if failure:
                raise failure[0]
            self._asgi = sub.routes[0].endpoint
            return self._asgi

    async def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5.0)
            except Exception:  # noqa: BLE001
                self._task.cancel()
        if self._api is not None:
            await self._api.client.aclose()
        self._asgi = self._task = self._api = self._stop = None

    # ---------------------------------------------------------------- ASGI
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        reply = self._guard(scope)
        if reply is None:
            try:
                handler = await self._ensure()
            except ImportError as exc:
                reply = JSONResponse({"error": f"MCP package not installed ({exc}). pip install mcp",
                                      "code": "mcp_missing"}, status_code=503)
            except Exception as exc:  # noqa: BLE001
                log.exception("MCP server could not start")
                reply = JSONResponse({"error": f"The MCP server could not start: {exc}", "code": "mcp_failed"},
                                     status_code=503)
        if reply is not None:
            await reply(scope, receive, send)
            return
        await handler(scope, receive, send)


def attach_mcp(app: Any, machine) -> MountedMcp:
    """Register ``/mcp`` on a FastAPI app (call BEFORE the static UI is mounted at ``/``)."""
    mounted = MountedMcp(app, machine)
    app.add_route("/mcp", mounted, include_in_schema=False)
    app.add_route("/mcp/", mounted, include_in_schema=False)
    app.on_event("shutdown")(mounted.close)
    app.state.mcp = mounted
    return mounted


__all__ = ["attach_mcp", "MountedMcp", "TOKEN_ENV"]
