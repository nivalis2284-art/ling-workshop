"""Desktop contracts with real OS locks, HTTP servers and application lifespans."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def lock_holder(data_dir):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            (
                "from pathlib import Path; import sys; "
                "from lingnexus.desktop.instance import InstanceLock; "
                "lock = InstanceLock(Path(sys.argv[1])); "
                "assert lock.acquire(); print('locked', flush=True); sys.stdin.readline()"
            ),
            str(data_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=ROOT,
    )
    assert child.stdout.readline().strip() == "locked", child.stderr.read()
    return child


def test_lock_is_exclusive_across_processes_and_released_on_crash(tmp_path):
    from lingnexus.desktop.instance import InstanceLock

    child = lock_holder(tmp_path)
    contender = InstanceLock(tmp_path)
    other_directory = InstanceLock(tmp_path / "other")
    try:
        assert not contender.acquire()
        assert other_directory.acquire()
        child.kill()
        child.wait(timeout=5)
        assert contender.acquire()
    finally:
        contender.release()
        other_directory.release()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_second_launch_reuses_live_session_and_quitting_it_does_not_stop_owner(tmp_path):
    from lingnexus.desktop.launcher import DesktopLauncher

    opened = []
    first = DesktopLauncher(tmp_path, port=0, browser=opened.append)
    second = DesktopLauncher(tmp_path, port=8765, browser=opened.append)
    try:
        primary = first.start()
        state_before = first.lock.state_path.read_bytes()
        duplicate = second.start()
        assert primary.primary is True
        assert duplicate.primary is False
        assert opened == [primary.url, primary.url]
        assert duplicate.url == primary.url
        assert first.lock.state_path.read_bytes() == state_before
        second.quit()
        with httpx.Client(trust_env=False) as client:
            assert client.get(primary.url.split("#")[0] + "health").status_code == 200
        assert urlsplit(primary.url).hostname == "127.0.0.1"
        assert parse_qs(urlsplit(primary.url).fragment)["token"][0]
    finally:
        second.quit()
        first.quit()
    assert not first.lock.state_path.exists()


def test_stale_session_is_replaced_only_after_acquiring_lock_and_data_survives(tmp_path):
    from lingnexus.desktop.instance import InstanceLock
    from lingnexus.desktop.launcher import DesktopLauncher, InstanceUnavailable

    (tmp_path / "owner-notes.txt").write_text("keep through recovery", encoding="utf-8")
    child = lock_holder(tmp_path)
    lock = InstanceLock(tmp_path)
    stale = {"version": 1, "pid": child.pid, "port": 1, "token": "old-session-" * 4}
    lock.state_path.write_text(json.dumps(stale), encoding="utf-8")
    waiting = DesktopLauncher(tmp_path, port=0, startup_timeout=0.2, browser=lambda url: None)
    try:
        with pytest.raises(InstanceUnavailable):
            waiting.start()
        assert json.loads(lock.state_path.read_text()) == stale
    finally:
        child.kill()
        child.communicate(timeout=5)
    recovered = DesktopLauncher(tmp_path, port=0, browser=lambda url: None)
    try:
        result = recovered.start()
        assert result.primary
        assert parse_qs(urlsplit(result.url).fragment)["token"][0] != stale["token"]
        assert (tmp_path / "owner-notes.txt").read_text() == "keep through recovery"
    finally:
        recovered.quit()


def test_duplicate_requires_matching_owner_secret_not_just_a_live_pid_and_port(tmp_path):
    from lingnexus.desktop.launcher import DesktopLauncher, InstanceUnavailable

    opened = []
    owner = DesktopLauncher(tmp_path, port=0, browser=opened.append)
    duplicate = DesktopLauncher(tmp_path, port=0, browser=opened.append, startup_timeout=0.2)
    try:
        owner.start()
        state_bytes = owner.lock.state_path.read_bytes()
        state = json.loads(state_bytes)
        state["token"] = "wrong-owner-session-" * 3
        owner.lock.state_path.write_text(json.dumps(state), encoding="utf-8")
        try:
            with pytest.raises(InstanceUnavailable):
                duplicate.start()
            assert len(opened) == 1
            assert json.loads(owner.lock.state_path.read_text()) == state
            with httpx.Client(trust_env=False) as client:
                assert client.get(f"http://127.0.0.1:{state['port']}/health").status_code == 200
        finally:
            owner.lock.state_path.write_bytes(state_bytes)
    finally:
        duplicate.quit()
        owner.quit()


def test_port_conflict_uses_another_loopback_port(tmp_path):
    from lingnexus.desktop.launcher import DesktopLauncher

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        preferred = occupied.getsockname()[1]
        launcher = DesktopLauncher(tmp_path, port=preferred, browser=lambda url: None)
        try:
            result = launcher.start()
            assert urlsplit(result.url).port != preferred
            origin = result.url.split("#")[0].rstrip("/")
            assert launcher.app.state.allowed_origins == {
                origin,
                origin.replace("127.0.0.1", "localhost"),
            }
        finally:
            launcher.quit()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-line parsing")
def test_startup_command_roundtrips_windows_paths_with_spaces_and_unicode():
    import ctypes
    from ctypes import wintypes

    from lingnexus.desktop.startup import startup_command

    command = startup_command(
        Path("C:/Users/Example Owner/Apps/灵/LingNexus.exe"),
        Path("C:/Users/Example Owner/AppData/Local/灵 data"),
        port=9876,
    )
    assert command.startswith('"C:')
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    try:
        assert list(argv[: count.value]) == [
            str(Path("C:/Users/Example Owner/Apps/灵/LingNexus.exe")),
            "--background",
            "--data-dir",
            str(Path("C:/Users/Example Owner/AppData/Local/灵 data")),
            "--port",
            "9876",
        ]
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(argv)


def test_tray_owns_lifetime_and_remote_quit_requires_owner_session(tmp_path):
    from lingnexus.desktop.launcher import DesktopLauncher

    tray_ready = threading.Event()
    tray_stopped = threading.Event()
    errors = []
    opened = []

    class Tray:
        def run(self, controller):
            controller.open_browser()
            tray_ready.set()
            assert tray_stopped.wait(10)

        def stop(self):
            tray_stopped.set()

    launcher = DesktopLauncher(tmp_path, port=0, browser=opened.append, tray_factory=Tray)

    def run():
        try:
            launcher.run()
        except Exception as error:  # noqa: BLE001 - report a worker's failure to the test
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert tray_ready.wait(10)
        assert len(opened) == 2
        base = opened[0].split("#")[0].rstrip("/")
        token = parse_qs(urlsplit(opened[0]).fragment)["token"][0]
        with httpx.Client(trust_env=False) as client:
            assert client.post(base + "/_desktop/quit").status_code == 401
            assert client.get(base + "/health").status_code == 200
            assert worker.is_alive()
            assert (
                client.post(
                    base + "/_desktop/quit",
                    headers={"Authorization": "Bearer " + token},
                ).status_code
                == 200
            )
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not errors
        assert not launcher.lock.state_path.exists()
        with pytest.raises(httpx.ConnectError), httpx.Client(trust_env=False) as client:
            client.get(base + "/health")
    finally:
        launcher.request_quit()
        worker.join(timeout=10)
        launcher.quit()


def test_quit_interrupts_actual_chat_run_and_preserves_database(tmp_path):
    from lingnexus.api.app import create_app
    from lingnexus.desktop.launcher import DesktopLauncher
    from lingnexus.models import ModelProfile
    from lingnexus.runtime import ChatRequest

    streaming = threading.Event()
    release_stream = threading.Event()
    key = "fictional-desktop-test-model-key-memory-only"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": waiting\n\n")
            self.wfile.flush()
            streaming.set()
            release_stream.wait(10)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    serving = threading.Thread(target=provider.serve_forever, daemon=True)
    serving.start()

    def factory(**kwargs):
        app = create_app(**kwargs)

        @app.post("/desktop-test/submit")
        async def submit():
            services = app.state.services
            agent = services.create_agent("Desktop test")
            profile = services.profiles.save(
                "Fixture",
                ModelProfile(
                    api_family="openai_chat_completions",
                    model="fixture",
                    base_url=f"http://127.0.0.1:{provider.server_port}",
                ),
                api_key=key,
            )
            return app.state.chat_runtime.submit(
                ChatRequest(
                    agent_id=agent,
                    message="wait",
                    request_id="quit-test",
                    profile_id=profile["id"],
                )
            )

        # Test-only route is added after the real app's catch-all static mount.
        app.router.routes.insert(0, app.router.routes.pop())
        return app

    launcher = DesktopLauncher(tmp_path, port=0, browser=lambda url: None, app_factory=factory)
    try:
        result = launcher.start()
        with httpx.Client(trust_env=False) as client:
            response = client.post(result.url.split("#")[0] + "desktop-test/submit")
            assert response.status_code == 200, response.text
            run = response.json()
        assert streaming.wait(5)
        launcher.quit()
        with sqlite3.connect(tmp_path / "ling.db") as db:
            assert db.execute(
                "SELECT status FROM chat_runs WHERE id=?", (run["id"],)
            ).fetchone() == ("interrupted",)
        assert (tmp_path / "ling.db").is_file()
        assert not launcher.lock.state_path.exists()
        assert all(
            key.encode() not in file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()
        )
    finally:
        release_stream.set()
        launcher.quit()
        provider.shutdown()
        provider.server_close()


def test_background_pause_is_disabled_without_route(tmp_path):
    from fastapi import FastAPI

    from lingnexus.desktop.launcher import DesktopLauncher

    launcher = DesktopLauncher(
        tmp_path, port=0, browser=lambda url: None, app_factory=lambda **kwargs: FastAPI()
    )
    try:
        launcher.start()
        assert launcher.background_enabled() is None
        assert launcher.set_background_enabled(False) is False
    finally:
        launcher.quit()


def test_background_pause_and_resume_use_authenticated_api_contract(tmp_path):
    from fastapi import FastAPI, Request

    from lingnexus.desktop.launcher import DesktopLauncher

    def factory(**kwargs):
        app = FastAPI()
        app.state.enabled = True

        @app.get("/api/background")
        async def status(request: Request):
            assert request.headers["Authorization"] == "Bearer " + kwargs["token"]
            return {"enabled": app.state.enabled}

        @app.put("/api/background")
        async def setting(request: Request):
            assert request.headers["Authorization"] == "Bearer " + kwargs["token"]
            assert request.headers["Origin"] in kwargs["allowed_origins"]
            value = await request.json()
            assert set(value) == {"enabled"}
            app.state.enabled = value["enabled"]
            return {"enabled": app.state.enabled}

        return app

    launcher = DesktopLauncher(tmp_path, port=0, browser=lambda url: None, app_factory=factory)
    try:
        launcher.start()
        assert launcher.background_enabled() is True
        assert launcher.set_background_enabled(False)
        assert launcher.background_enabled() is False
        assert launcher.set_background_enabled(True)
        assert launcher.background_enabled() is True
    finally:
        launcher.quit()


def test_tray_background_setting_persists_through_actual_app_restart(tmp_path):
    from lingnexus.desktop.launcher import DesktopLauncher

    first = DesktopLauncher(tmp_path, port=0, browser=lambda url: None)
    second = DesktopLauncher(tmp_path, port=0, browser=lambda url: None)
    try:
        first.start()
        assert first.background_enabled() is True
        assert first.set_background_enabled(False)
        first.quit()
        second.start()
        assert second.background_enabled() is False
        assert second.set_background_enabled(True)
        assert second.background_enabled() is True
    finally:
        first.quit()
        second.quit()
