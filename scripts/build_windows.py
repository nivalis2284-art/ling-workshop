"""Build a per-user Windows package without installing tools or changing the venv.

Run from the locked project environment: python scripts/build_windows.py --smoke
Inno Setup is an explicit prerequisite; --skip-installer builds just the folder.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
DIST = PACKAGING / "dist" / "LingNexus"


class BuildError(RuntimeError):
    pass


def run(command: list[str | Path], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    result = subprocess.run([str(arg) for arg in command], cwd=cwd, env=env, check=False)
    if result.returncode:
        raise BuildError(f"Build command failed with exit code {result.returncode}: {command[0]}")


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for location in (Path.home() / ".local/bin/uv.exe", Path.home() / ".cargo/bin/uv.exe"):
        if location.is_file():
            return str(location)
    return None


def find_iscc(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise BuildError(f"Inno Setup compiler does not exist: {explicit}")
        return explicit.resolve()
    found = shutil.which("ISCC.exe")
    if found:
        return Path(found)
    directories = [
        Path(value)
        for name in ("ProgramFiles", "ProgramFiles(x86)")
        if (value := os.environ.get(name))
    ]
    if local := os.environ.get("LOCALAPPDATA"):
        directories.append(Path(local) / "Programs")
    for directory in directories:
        for version in ("7", "6"):
            compiler = directory / f"Inno Setup {version}" / "ISCC.exe"
            if compiler.is_file():
                return compiler
    return None


def prerequisites() -> tuple[str, str, Path, dict]:
    if sys.platform != "win32":
        raise BuildError("Build the Windows executable on Windows with Python 3.12.")
    uv = find_uv()
    npm = shutil.which("npm.cmd")
    node = shutil.which("node.exe")
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not uv:
        raise BuildError("uv is required to validate uv.lock and use the existing locked venv.")
    if not npm or not node:
        raise BuildError(
            "Node.js and npm are required. Install a version supported by frontend/package.json."
        )
    if not python.is_file():
        raise BuildError(
            "The project .venv is missing. The parent must run uv sync --locked --extra dev first."
        )
    if not (ROOT / "frontend" / "node_modules" / ".bin" / "vite.cmd").is_file():
        raise BuildError(
            "Frontend dependencies are missing. Run npm ci in frontend before building."
        )
    node_version = subprocess.check_output([node, "--version"], text=True).strip()
    parts = tuple(int(part) for part in node_version.lstrip("v").split("."))
    if not ((20, 19, 0) <= parts < (21, 0, 0) or parts >= (22, 12, 0)):
        raise BuildError("Node.js must be ^20.19.0 or >=22.12.0 for the current frontend.")
    # Check freshness read-only, then never synchronize or mutate the parent's environment.
    run([uv, "lock", "--check", "--offline"])
    run([uv, "sync", "--check", "--locked", "--extra", "dev", "--offline"])
    code = (
        "import importlib.metadata as m,json,sys; "
        "print(json.dumps({'python':list(sys.version_info[:3]), 'packages':"
        "{n:m.version(n) for n in ('pyinstaller','pystray','fastapi','uvicorn','Pillow')}}))"
    )
    result = subprocess.run([python, "-c", code], text=True, capture_output=True, check=False)
    if result.returncode:
        raise BuildError(
            "Required packaging dependencies are absent from .venv. Parent: uv sync --locked --extra dev."
        )
    environment = json.loads(result.stdout)
    if environment["python"][:2] != [3, 12]:
        raise BuildError("The locked build environment must use Python 3.12.")
    with (ROOT / "uv.lock").open("rb") as file:
        lock = tomllib.load(file)
    versions = {item["name"].lower(): item.get("version") for item in lock["package"]}
    for name, version in environment["packages"].items():
        if versions.get(name.lower()) != version:
            raise BuildError(
                f"Installed {name} does not match uv.lock. Parent: uv sync --locked --extra dev."
            )
    environment["node"] = node_version
    environment["uv_lock_sha256"] = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    return uv, npm, python, environment


def installer(compiler: Path, version: str) -> None:
    if not (DIST / "LingNexus.exe").is_file():
        raise BuildError(
            "The one-folder package is missing. Run the full build before --installer-only."
        )
    run(
        [
            compiler,
            f"/DAppVersion={version}",
            f"/DBundleDir={DIST}",
            f"/DInstallerOutput={PACKAGING / 'installer'}",
            PACKAGING / "windows" / "LingNexus.iss",
        ]
    )


def smoke() -> Path:
    """Exercise the actual windowless exe, tray, duplicate launch, assets and Quit."""
    data = Path(tempfile.mkdtemp(prefix="smoke-", dir=PACKAGING))
    executable = DIST / "LingNexus.exe"
    # The isolated smoke instance needs Windows paths, never ambient model credentials.
    allowed_environment = {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_environment
    }
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process = subprocess.Popen(
        [executable, "--background", "--data-dir", data, "--port", "0"],
        cwd=DIST,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        state_path = data / ".desktop" / "owner-session.json"
        deadline = time.monotonic() + 30
        while not state_path.is_file():
            if process.poll() is not None or time.monotonic() > deadline:
                raise BuildError("The packaged executable did not publish a live session.")
            time.sleep(0.1)
        state_bytes = state_path.read_bytes()
        state = json.loads(state_bytes)
        base = f"http://127.0.0.1:{state['port']}"
        opener = build_opener(ProxyHandler({}))
        with opener.open(base + "/health", timeout=5) as response:
            if json.load(response).get("status") != "ok":
                raise BuildError("Packaged health check failed.")
        with opener.open(base + "/", timeout=5) as response:
            if b"<html" not in response.read().lower():
                raise BuildError("The packaged frontend was not served.")
        request = Request(
            base + "/_desktop/health", headers={"Authorization": "Bearer " + state["token"]}
        )
        with opener.open(request, timeout=5) as response:
            if json.load(response).get("pid") != process.pid:
                raise BuildError("The packaged server is not owned by the launcher process.")
        with sqlite3.connect(data / "ling.db") as db:
            migrated = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        expected = {path.name for path in (ROOT / "src/lingnexus/storage/migrations").glob("*.sql")}
        if not expected or migrated != expected:
            raise BuildError("Bundled migrations were not applied.")
        duplicate = subprocess.run(
            [executable, "--background", "--data-dir", data],
            timeout=20,
            check=False,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if duplicate.returncode or state_path.read_bytes() != state_bytes:
            raise BuildError("The packaged duplicate launch did not reuse the owner session.")
        run([executable, "--quit", "--data-dir", data], env=environment)
        if process.wait(timeout=20) != 0 or state_path.exists() or not (data / "ling.db").is_file():
            raise BuildError("Packaged Quit failed to stop the process and preserve data.")
        try:
            opener.open(base + "/health", timeout=1).close()
        except URLError:
            pass
        else:
            raise BuildError("The packaged server remains reachable after Quit.")
        print(f"Executable smoke test passed; preserved test data: {data}")
        return data
    finally:
        if process.poll() is None:
            subprocess.run(
                [executable, "--quit", "--data-dir", data],
                timeout=20,
                check=False,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()  # Only this script's own isolated test process.
                process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Validate prerequisites without building"
    )
    parser.add_argument(
        "--skip-installer", action="store_true", help="Build the one-folder package only"
    )
    parser.add_argument(
        "--installer-only", action="store_true", help="Compile an existing one-folder package"
    )
    parser.add_argument(
        "--iscc", type=Path, help="Explicit path to the installed Inno Setup compiler"
    )
    parser.add_argument(
        "--smoke", action="store_true", help="Verify the actual packaged executable"
    )
    args = parser.parse_args(argv)
    if args.installer_only and (args.skip_installer or args.check):
        parser.error("--installer-only cannot be combined with --skip-installer or --check")
    try:
        with (ROOT / "pyproject.toml").open("rb") as file:
            version = tomllib.load(file)["project"]["version"]
        compiler = find_iscc(args.iscc)
        if not args.installer_only:
            uv, npm, python, environment = prerequisites()
            print(
                "Validated the existing locked environment; no dependency installation performed."
            )
            if args.check:
                print(json.dumps(environment, indent=2))
                print(f"Inno Setup compiler: {compiler or 'MISSING'}")
                return 0 if compiler or args.skip_installer else 2
            # Frontend compilation must precede Analysis collecting its output.
            run([npm, "run", "build"], cwd=ROOT / "frontend")
            icon_code = (
                "import sys; sys.path.insert(0,'src'); "
                "from lingnexus.desktop.tray import icon_image; "
                "icon_image().save('packaging/windows/LingNexus.ico',"
                "sizes=[(16,16),(32,32),(48,48),(64,64)])"
            )
            run([python, "-c", icon_code])
            run(
                [
                    uv,
                    "run",
                    "--locked",
                    "--no-sync",
                    "--extra",
                    "dev",
                    "python",
                    "-m",
                    "PyInstaller",
                    "--noconfirm",
                    "--distpath",
                    PACKAGING / "dist",
                    "--workpath",
                    PACKAGING / "build",
                    PACKAGING / "windows" / "LingNexus.spec",
                ]
            )
            (DIST / "build-info.json").write_text(
                json.dumps(environment, indent=2), encoding="utf-8"
            )
            print(f"One-folder package: {DIST}")
        if args.smoke:
            smoke()
        if args.skip_installer:
            return 0
        if compiler is None:
            print(
                "Installer prerequisite missing: install Inno Setup 6.3 or later separately, then run "
                "python scripts/build_windows.py --installer-only --iscc <path-to-ISCC.exe>. "
                "The compiler was not downloaded or installed.",
                file=sys.stderr,
            )
            return 2
        installer(compiler, version)
        print(f"Installer output: {PACKAGING / 'installer'}")
        return 0
    except (BuildError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Windows build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
