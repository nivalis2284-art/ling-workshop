"""PyInstaller script entry; kept separate from relative package imports."""

from lingnexus.desktop.main import main

if __name__ == "__main__":
    raise SystemExit(main())
