"""Bounded workspace scanning: limits, deterministic versions and refusals."""

import hashlib
import os
from pathlib import Path

import pytest

from lingnexus.common import workspace_scan as scanning
from lingnexus.tools._paths import FileAccessDenied


def test_scan_reports_deterministic_version_and_optional_content(tmp_path):
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_bytes(b"print('x')\n")
    (root / "readme.md").write_bytes(b"# readme\n")
    (root / "empty.bin").write_bytes(b"")

    first = scanning.scan(root)
    second = scanning.scan(root)

    assert first == second
    assert first["count"] == 3
    assert first["bytes"] == len(b"print('x')\n") + len(b"# readme\n")
    assert first["files"] == {}
    assert set(first["hashes"]) == {"pkg/mod.py", "readme.md", "empty.bin"}
    assert first["hashes"]["pkg/mod.py"] == hashlib.sha256(b"print('x')\n").hexdigest()
    assert first["hashes"]["empty.bin"] == hashlib.sha256(b"").hexdigest()
    assert first["version"].startswith(scanning.SCAN_POLICY_VERSION + ":")

    with_content = scanning.scan(root, include_content=True)

    assert with_content["files"]["readme.md"] == b"# readme\n"
    assert with_content["hashes"] == first["hashes"]
    assert with_content["version"] == first["version"]


def test_scan_accepts_two_thousand_and_one_files_over_one_mib(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for index in range(2001):
        (root / f"f{index:04d}.txt").write_bytes(b"x" * 600)

    result = scanning.scan(root)

    assert result["count"] == 2001
    assert len(result["hashes"]) == 2001
    assert result["bytes"] == 2001 * 600
    assert result["bytes"] > 1_048_576
    assert result["files"] == {}


def test_scan_limits_are_monkeypatchable_with_static_codes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    for index in range(3):
        (root / f"f{index}.txt").write_bytes(b"12345678")

    monkeypatch.setattr(scanning, "MAX_FILES", 2)
    with pytest.raises(scanning.ScanError) as too_many:
        scanning.scan(root)
    assert too_many.value.code == "file_count"
    assert isinstance(too_many.value, ValueError)

    monkeypatch.setattr(scanning, "MAX_FILES", 10_000)
    monkeypatch.setattr(scanning, "MAX_TOTAL", 16)
    with pytest.raises(scanning.ScanError) as too_big:
        scanning.scan(root)
    assert too_big.value.code == "total_bytes"

    monkeypatch.setattr(scanning, "MAX_TOTAL", 64 * 1_048_576)
    monkeypatch.setattr(scanning, "MAX_FILE", 4)
    with pytest.raises(scanning.ScanError) as big_file:
        scanning.scan(root)
    assert big_file.value.code == "file_bytes"

    monkeypatch.setattr(scanning, "MAX_FILE", 8 * 1_048_576)
    monkeypatch.setattr(scanning, "MAX_ENTRIES", 2)
    with pytest.raises(scanning.ScanError) as too_many_entries:
        scanning.scan(root)
    assert too_many_entries.value.code == "entry_count"


def test_scan_rehashes_same_size_change_with_restored_mtime(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    target = root / "same.txt"
    target.write_bytes(b"AAAA")
    first = scanning.scan(root)
    original = target.stat()

    target.write_bytes(b"BBBB")
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    restored = target.stat()
    assert restored.st_size == original.st_size
    if restored.st_mtime_ns != original.st_mtime_ns:
        pytest.skip("filesystem does not preserve the restored mtime")

    second = scanning.scan(root)

    assert second["hashes"]["same.txt"] == hashlib.sha256(b"BBBB").hexdigest()
    assert second["hashes"]["same.txt"] != first["hashes"]["same.txt"]
    assert second["version"] != first["version"]


def test_scan_excludes_noise_trees_and_env_files(tmp_path):
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    (root / "keep.txt").write_bytes(b"keep")
    (root / "sub" / "keep2.txt").write_bytes(b"keep2")
    for name in (".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".pytest_cache", ".ruff_cache", ".tmp", ".home", ".deps", ".artifacts"):
        folder = root / name
        folder.mkdir()
        (folder / "noise.bin").write_bytes(b"noise")
    (root / ".env").write_bytes(b"SECRET=1")
    (root / ".env.local").write_bytes(b"SECRET=2")
    (root / "sub" / "NODE_MODULES").mkdir()
    (root / "sub" / "NODE_MODULES" / "noise.bin").write_bytes(b"noise")

    first = scanning.scan(root)

    assert set(first["hashes"]) == {"keep.txt", "sub/keep2.txt"}
    assert first["count"] == 2

    (root / ".git" / "more.bin").write_bytes(b"more")
    (root / ".env.local").write_bytes(b"SECRET=3")
    assert scanning.scan(root)["version"] == first["version"]

    (root / "keep.txt").write_bytes(b"keep!")
    assert scanning.scan(root)["version"] != first["version"]

    assert scanning.excluded(Path(".git/config"))
    assert scanning.excluded("A/.VENV/lib")
    assert scanning.excluded("sub/node_modules/thing")
    assert scanning.excluded(".env")
    assert scanning.excluded(".env.production")
    assert not scanning.excluded("keep.txt")
    assert not scanning.excluded(".gitignore")


def test_scan_refuses_links_and_hard_links(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "real.txt").write_bytes(b"real")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    checked = 0

    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pass
    else:
        if link.is_symlink():
            with pytest.raises(FileAccessDenied):
                scanning.scan(root)
            assert outside.read_bytes() == b"outside"
            link.unlink()
            checked += 1

    alias = root / "alias.txt"
    try:
        os.link(root / "real.txt", alias)
    except (OSError, NotImplementedError):
        pass
    else:
        if alias.exists():
            with pytest.raises(FileAccessDenied):
                scanning.scan(root)
            assert (root / "real.txt").read_bytes() == b"real"
            checked += 1

    if checked == 0:
        pytest.skip("neither symlinks nor hard links are available")
