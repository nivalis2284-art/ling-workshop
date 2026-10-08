"""One local browser session and one explicitly owned server per data directory."""

import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from .instance import InstanceLock, Session
from .server import ServerHost, bind_loopback, live_session, local_request
from .startup import LoginStartup


class InstanceUnavailable(RuntimeError):
    """The lock owner has not yet published a healthy desktop session."""


@dataclass(frozen=True)
class LaunchResult:
    primary: bool
    url: str = field(repr=False)


def frontend_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(vars(sys)["_MEIPASS"]) / "frontend"
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


class DesktopLauncher:
    def __init__(
        self,
        data_dir: Path,
        *,
        port: int = 8765,
        browser: Callable[[str], object] = webbrowser.open,
        tray_factory: Callable | None = None,
        app_factory: Callable[..., FastAPI] | None = None,
        startup_timeout: float = 15,
        shutdown_timeout: float = 15,
    ):
        if not 0 <= port < 65536:
            raise ValueError("Desktop port must be between 0 and 65535.")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.port = port
        self.browser = browser
        self.tray_factory = tray_factory
        self.app_factory = app_factory
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.lock = InstanceLock(self.data_dir)
        self.startup = LoginStartup(self.data_dir, port)
        self.session: Session | None = None
        self.app: FastAPI | None = None
        self._host: ServerHost | None = None
        self._tray = None
        self._stopping = threading.Event()
        self._quit_lock = threading.Lock()

    def start(self, *, open_browser: bool = True) -> LaunchResult:
        deadline = time.monotonic() + self.startup_timeout
        while not self.lock.acquire():
            existing = self.lock.read_session()
            if existing is not None and live_session(existing):
                self.session = existing
                if open_browser:
                    self.open_browser()
                return LaunchResult(False, existing.url)
            if time.monotonic() >= deadline:
                raise InstanceUnavailable(
                    "LingNexus is already starting or stopping. Try again shortly."
                )
            self._stopping.wait(0.05)
        sock = None
        try:
            from .restore import apply_pending_restore

            apply_pending_restore(self.data_dir)
            sock = bind_loopback(self.port)
            session = Session(os.getpid(), sock.getsockname()[1], secrets.token_urlsafe(32))
            if self.app_factory is None:
                from lingnexus.api.app import create_app

                factory = create_app
            else:
                factory = self.app_factory
            self.app = factory(
                data_dir=self.data_dir,
                token=session.token,
                frontend_dir=frontend_directory(),
                allowed_origins={session.origin, f"http://localhost:{session.port}"},
            )
            self.session = session
            self.app.state.request_quit = self.request_quit
            self.app.state.login_startup = self.startup
            self._host = ServerHost(self.app, session, sock, self.request_quit)
            self._host.start()
            while not live_session(session):
                if not self._host.thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("LingNexus could not start its local server.")
                self._stopping.wait(0.05)
            self.lock.write_session(session)
        except BaseException:
            self.quit()
            if sock is not None:
                sock.close()
            raise
        if open_browser:
            self.open_browser()
        return LaunchResult(True, session.url)

    def open_browser(self) -> None:
        if self.session is None:
            raise RuntimeError("The desktop session has not started.")
        self.browser(self.session.url)

    def run(self, *, open_browser: bool = True) -> int:
        try:
            result = self.start(open_browser=open_browser)
            if not result.primary:
                return 0
            if self.tray_factory is None:
                from .tray import WindowsTray

                self._tray = WindowsTray()
            else:
                self._tray = self.tray_factory()
            # A server that exits unexpectedly must also end the tray lifetime.
            watcher = threading.Thread(
                target=self._watch_server, name="LingNexus lifetime", daemon=True
            )
            watcher.start()
            self._tray.run(self)
            return 1 if self._host is not None and self._host.failed else 0
        finally:
            self.quit()

    def _watch_server(self) -> None:
        if self._host is not None:
            self._host.thread.join()
            self.request_quit()

    def request_quit(self) -> None:
        self._stopping.set()
        if self._host is not None:
            self._host.request_stop()
        if self._tray is not None:
            self._tray.stop()

    def quit(self) -> None:
        with self._quit_lock:
            self.request_quit()
            if self._host is not None:
                self._host.stop(self.shutdown_timeout)
                if self.session is not None:
                    self.lock.clear_session(self.session)
                self._host = None
            self.lock.release()

    def background_enabled(self) -> bool | None:
        if self.session is None:
            return None
        value = local_request(self.session, "/api/background")
        enabled = value.get("enabled") if value is not None else None
        return enabled if isinstance(enabled, bool) else None

    def set_background_enabled(self, enabled: bool) -> bool:
        if self.session is None or self.background_enabled() is None:
            return False
        result = local_request(
            self.session, "/api/background", method="PUT", body={"enabled": enabled}, timeout=2
        )
        return result is not None and result.get("enabled") is enabled


def quit_existing(data_dir: Path, *, timeout: float = 15) -> bool:
    """Graceful installer/CLI shutdown; never kill a PID taken from a stale file."""
    lock = InstanceLock(data_dir)
    deadline = time.monotonic() + timeout
    requested = False
    while time.monotonic() < deadline:
        if lock.acquire():
            lock.release()
            return True
        session = lock.read_session()
        if not requested and session is not None and live_session(session):
            requested = local_request(session, "/_desktop/quit", method="POST") is not None
        time.sleep(0.05)
    return False
