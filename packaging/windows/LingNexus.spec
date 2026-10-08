# Official reference: https://pyinstaller.org/en/stable/spec-files.html
from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]
frontend = root / "frontend" / "dist"
if not (frontend / "index.html").is_file():
    raise SystemExit("Build the frontend with npm run build before packaging.")

analysis = Analysis(
    [str(root / "packaging" / "windows" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (str(frontend), "frontend"),
        (str(root / "src" / "lingnexus" / "storage" / "migrations" / "*.sql"),
         "lingnexus/storage/migrations"),
    ],
    hiddenimports=["pystray._win32", "uvicorn.logging", "uvicorn.loops.asyncio",
                   "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "ruff", "pyright", "tkinter"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True, name="LingNexus",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=True,
    icon=str(root / "packaging" / "windows" / "LingNexus.ico"),
)
collection = COLLECT(
    executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="LingNexus",
)
