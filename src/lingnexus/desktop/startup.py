"""Opt-in login startup for the current Windows user only."""

import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LingNexus"


def startup_command(
    executable: Path, data_dir: Path, *, port: int = 8765, source: bool = False
) -> str:
    executable = Path(executable)
    if '"' in str(executable) or not executable.is_absolute():
        raise ValueError("Login startup requires an absolute executable path.")
    if not 0 <= port < 65536:
        raise ValueError("Invalid desktop port.")
    args = ["-m", "lingnexus.desktop"] if source else []
    args += [
        "--background",
        "--data-dir",
        str(Path(data_dir).expanduser().resolve()),
        "--port",
        str(port),
    ]
    # Windows Run must quote the executable even when its path has no spaces.
    return f'"{executable}" ' + subprocess.list2cmdline(args)


class LoginStartup:
    def __init__(self, data_dir: Path, port: int = 8765):
        frozen = bool(getattr(sys, "frozen", False))
        executable = Path(sys.executable)
        if not frozen and executable.name.lower() == "python.exe":
            windowless = executable.with_name("pythonw.exe")
            if windowless.is_file():
                executable = windowless
        self.command = startup_command(executable, data_dir, port=port, source=not frozen)

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def enabled(self) -> bool:
        if not self.available:
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, VALUE_NAME)
                return value == self.command
        except FileNotFoundError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        if not self.available:
            raise OSError("Login startup is available on Windows.")
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, access=winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, self.command)
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
