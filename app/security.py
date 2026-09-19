"""Keep browser requests within the local, single-user deployment boundary."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class LocalRequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1"}
        self.allowed_hosts.update(
            host.strip().lower()
            for host in os.environ.get("STL_TO_STEP_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        name = os.environ.get("CODESPACE_NAME")
        domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
        self.codespace_host = f"{name}-8421.{domain}".lower() if name and domain else None
        if self.codespace_host:
            self.allowed_hosts.add(self.codespace_host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        try:
            hostname = urlsplit(f"//{host}").hostname
        except ValueError:
            hostname = None
        if hostname not in self.allowed_hosts:
            await JSONResponse({"detail": "Unrecognized server hostname"}, 400)(
                scope, receive, send,
            )
            return
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            origin = headers.get("origin")
            allowed_origins = {f"http://{host}", f"https://{host}"}
            if self.codespace_host:
                allowed_origins.add(f"https://{self.codespace_host}")
            if (
                origin is not None and origin.lower() not in allowed_origins
            ) or headers.get("sec-fetch-site") == "cross-site":
                await JSONResponse({"detail": "Cross-site requests are not allowed"}, 403)(
                    scope, receive, send,
                )
                return
        await self.app(scope, receive, send)
