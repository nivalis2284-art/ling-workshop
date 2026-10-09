"""Install, upgrade and uninstall only the owned disposable LingNexus test install.

Refuses to run when the application is already installed for this owner.
Uses prepared synthetic test data outside the installation directory.
"""

import hashlib
import json
import os
import subprocess
import time
import tomllib
import winreg
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / ".artifacts/install-test"
DATA = ROOT / ".artifacts/install-data"
UNINSTALL_KEY = chr(92).join(
    (
        "Software",
        "Microsoft",
        "Windows",
        "CurrentVersion",
        "Uninstall",
        "{051207B2-6843-4D0F-951E-FB21F59D4C17}_is1",
    )
)
RUN_KEY = chr(92).join(("Software", "Microsoft", "Windows", "CurrentVersion", "Run"))


def registry(key, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except FileNotFoundError:
        return None


def run(argv):
    result = subprocess.run(
        [str(arg) for arg in argv],
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    assert result.returncode == 0, f"Test command returned {result.returncode}"


def footprint():
    files = [
        DATA / "ling.db",
        *sorted((DATA / "attachments").glob("*")),
        *sorted((DATA / "task-data").rglob("*")),
    ]
    return {
        file.relative_to(DATA).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in files
        if file.is_file()
    }


def launch_check():
    process = subprocess.Popen(
        [str(INSTALL / "LingNexus.exe"), "--background", "--data-dir", str(DATA), "--port", "0"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        session_file = DATA / ".desktop/owner-session.json"
        deadline = time.monotonic() + 30
        while not session_file.exists():
            assert process.poll() is None and time.monotonic() < deadline, "No packaged session"
            time.sleep(0.1)
        session = json.loads(session_file.read_text("utf-8"))
        base = f"http://127.0.0.1:{session['port']}"
        with httpx.Client(
            base_url=base,
            trust_env=False,
            timeout=30,
            headers={"Authorization": "Bearer " + session["token"]},
        ) as api:
            assert api.get("/health").json()["status"] == "ok"
            assert "<html" in api.get("/").text.lower()
            agents = api.get("/api/agents").json()
            assert agents, "Fixture lost its individual"
            tasks = api.get("/api/tasks").json()
            completed = [
                task for task in tasks if task["status"] == "completed" and task["verified"]
            ]
            assert completed, "Fixture lost actual verified task"
            response = api.get("/api/tasks/" + completed[0]["id"] + "/artifacts/calculator.py")
            assert response.status_code == 200 and b"a+b" in response.content.replace(b" ", b"")
            assert all(not p["credentials_available"] for p in api.get("/api/models").json())
            return {
                "agents": len(agents),
                "completed_tasks": len(completed),
                "task_artifact_available": True,
                "credentials_not_persisted": True,
            }
    finally:
        run([INSTALL / "LingNexus.exe", "--quit", "--data-dir", DATA])
        assert process.wait(timeout=30) == 0


def main():
    assert os.name == "nt"
    assert registry(UNINSTALL_KEY, "InstallLocation") is None, (
        "Existing owner install must be preserved"
    )
    assert not INSTALL.exists(), "Use a fresh disposable install folder"
    assert (DATA / "ling.db").is_file(), "Prepare synthetic fixture before install"
    startup = registry(RUN_KEY, "LingNexus")
    with (ROOT / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    installer = ROOT / "packaging/installer" / f"LingNexus-{version}-Setup.exe"
    install_args = [
        installer,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LANG=chinesesimp",
        "/DIR=" + str(INSTALL),
        "/GROUP=LingNexus validation",
        "/TASKS=",
        "/LOG=" + str(ROOT / ".artifacts/install.log"),
    ]
    try:
        run(install_args)
        assert registry(UNINSTALL_KEY, "InstallLocation").rstrip(r"\/") == str(INSTALL)
        first = launch_check()
        before_upgrade = footprint()
        run(install_args)
        assert footprint() == before_upgrade, "Upgrade modified persistent data"
        upgraded = launch_check()
        before_uninstall = footprint()
        run([INSTALL / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        assert not (INSTALL / "LingNexus.exe").exists()
        assert footprint() == before_uninstall, "Uninstall modified persistent data"
        assert registry(UNINSTALL_KEY, "InstallLocation") is None
        assert registry(RUN_KEY, "LingNexus") == startup, "Unexpected login startup mutation"
        result = {
            "status": "passed",
            "installed_runtime": first,
            "upgraded_runtime": upgraded,
            "upgrade_data_unchanged": True,
            "uninstall_data_unchanged": True,
            "login_startup_unchanged": True,
            "data_files_preserved": len(before_uninstall),
        }
        (ROOT / ".artifacts/installer-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result))
    finally:
        uninstaller = INSTALL / "unins000.exe"
        if uninstaller.is_file() and registry(UNINSTALL_KEY, "InstallLocation"):
            run([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])


if __name__ == "__main__":
    main()
