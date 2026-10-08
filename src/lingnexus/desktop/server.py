"""Launcher-owned uvicorn lifetime and an authenticated loopback control boundary."""

import errno
import json
import secrets
import socket
import threading
from collections.abc import Callable
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi import Request as APIRequest

from .instance import Session, process_alive


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_request(
    session: Session,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = 0.5,
) -> dict | None:
    request = Request(
        session.origin + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + session.token,
            "Origin": session.origin,
            "Content-Type": "application/json",
            "Connection": "close",
        },
    )
    try:
        # Bypass system proxies; never forward an owner session to a redirect.
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            if response.headers.get_content_type() != "application/json":
                return None
            value = json.loads(response.read(65536))
            return value if isinstance(value, dict) else None
    except (URLError, OSError, ValueError):
        return None


def live_session(session: Session) -> bool:
    if not process_alive(session.pid):
        return False
    value = local_request(session, "/_desktop/health")
    return value is not None and value.get("status") == "ok" and value.get("pid") == session.pid


def bind_loopback(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as error:
            if not port or error.errno != errno.EADDRINUSE:
                raise
            sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        return sock
    except BaseException:
        sock.close()
        raise


class ServerHost:
    def __init__(
        self, app: FastAPI, session: Session, sock: socket.socket, request_quit: Callable[[], None]
    ):
        self.socket = sock
        self.failed = False

        def require_session(request: APIRequest) -> None:
            if request.headers.get("origin", session.origin) != session.origin:
                raise HTTPException(403, "Unapproved desktop control origin.")
            if not secrets.compare_digest(
                request.headers.get("authorization", "").encode(),
                ("Bearer " + session.token).encode(),
            ):
                raise HTTPException(401, "Open LingNexus from its local launcher.")

        @app.get("/_desktop/health", include_in_schema=False)
        async def health(request: APIRequest):
            require_session(request)
            return {"status": "ok", "pid": session.pid}

        @app.post("/_desktop/quit", include_in_schema=False)
        async def quit_server(request: APIRequest):
            require_session(request)
            request_quit()
            return {"status": "stopping"}

        # create_app mounts static / last. Desktop controls must precede that mount.
        controls = [
            route
            for route in app.router.routes
            if getattr(route, "path", "").startswith("/_desktop/")
        ]
        app.router.routes[:] = controls + [
            route for route in app.router.routes if route not in controls
        ]
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=session.port,
                log_config=None,
                access_log=False,
                log_level="warning",
                lifespan="on",
                loop="asyncio",
                http="h11",
                ws="none",
                timeout_graceful_shutdown=2,
            )
        )
        self.thread = threading.Thread(target=self._run, name="LingNexus server", daemon=False)

    def _run(self) -> None:
        try:
            self.server.run(sockets=[self.socket])
        except BaseException:  # noqa: BLE001 - thread boundary must include uvicorn SystemExit
            # Exceptions can contain request/credential data; expose only failure status.
            self.failed = True
        finally:
            self.socket.close()

    def start(self) -> None:
        self.thread.start()

    def request_stop(self) -> None:
        self.server.should_exit = True

    def stop(self, timeout: float = 15) -> None:
        self.request_stop()
        if self.thread.ident is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                # Retain the OS lock and session while a real server still exists.
                raise RuntimeError("LingNexus is still shutting down.")
        self.socket.close()
