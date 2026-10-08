"""Source and frozen entry point for the local desktop application."""

import argparse
import sys
from pathlib import Path

from .launcher import DesktopLauncher, quit_existing


def _show_error(message: str, *, background: bool) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if sys.platform == "win32" and not background:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "LingNexus", 0x10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LingNexus local browser and background launcher")
    parser.add_argument("--data-dir", type=Path, help="Persistent owner data directory")
    parser.add_argument(
        "--port", type=int, default=8765, help="Preferred loopback port; 0 selects a free port"
    )
    parser.add_argument(
        "--background", action="store_true", help="Start the tray without opening a browser"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Start the tray without opening a browser"
    )
    parser.add_argument(
        "--quit", action="store_true", help="Gracefully stop the instance for this data directory"
    )
    args = parser.parse_args(argv)
    from lingnexus.api.app import default_data_dir

    directory = args.data_dir or default_data_dir()
    if args.quit:
        return 0 if quit_existing(directory) else 1
    try:
        return DesktopLauncher(directory, port=args.port).run(
            open_browser=not (args.background or args.no_browser),
        )
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - process UI boundary must never echo tokens or model secrets
        _show_error(
            "LingNexus 无法启动或正在退出。请稍后重试，并确认数据目录可访问。",
            background=args.background or args.no_browser,
        )
        return 1
