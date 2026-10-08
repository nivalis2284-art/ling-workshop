"""Offline image resolution, snapshot binding and trusted install prerequisites."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.tools._paths import check_tree, directory, open_file
from lingnexus.tools._process import run_process
from lingnexus.tools.docker import ExecutionResult

from .policy import DependencyCandidate, package_parts


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def image_id(reference: str) -> str:
    if type(reference) is not str or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]*", reference):
        raise PermissionDenied("The task needs a trusted local Docker image.")
    result = run_process([
        "docker", "image", "inspect", "--format",
        '{"os":{{json .Os}},"id":{{json .Id}},"volumes":{{json (index .Config "Volumes")}}}',
        reference,
    ], timeout=5, max_output_bytes=16_384)
    try:
        value = json.loads(result.stdout) if result.ok and not result.truncated else {}
    except (ValueError, UnicodeError):
        value = {}
    if (not isinstance(value, dict) or value.get("os") != "linux"
            or "volumes" not in value or value["volumes"] not in (None, {})
            or not isinstance(value.get("id"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["id"])):
        raise PermissionDenied("The task image must already exist locally, be Linux and have no volumes.")
    return value["id"]


def snapshot_binding(root: Path) -> dict:
    """Include config/manifest files, directories and inode identity; never follow links."""
    check_tree(root)
    info = root.lstat()
    entries, total = {}, 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in sorted([*dirs, *files]):
            path = Path(folder) / name
            metadata = path.lstat()
            item = {"mode": stat.S_IMODE(metadata.st_mode), "directory": path.is_dir()}
            if not item["directory"]:
                total += metadata.st_size
                if total > 256 * 1_048_576:
                    raise ValueError("Dependency snapshot exceeds the 256 MiB review limit.")
                hasher = hashlib.sha256()
                with open_file(path, "read") as stream:
                    while chunk := stream.read(65_536):
                        hasher.update(chunk)
                item["sha256"] = hasher.hexdigest()
                item["size"] = metadata.st_size
            entries[path.relative_to(root).as_posix()] = item
    return {
        "path": str(root), "device": info.st_dev, "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode), "manifest_hash": digest(entries),
    }


def check_npm_project(root: Path, candidate: DependencyCandidate) -> None:
    """Do not let snapshot npm configuration add unapproved direct dependencies."""
    if candidate.manager != "npm":
        return
    for name in (".npmrc", "npm-shrinkwrap.json"):
        if (root / name).exists():
            raise PermissionDenied("Remove project npm configuration/shrinkwrap before preparing an install.")
    manifest = root / "package.json"
    if not manifest.exists():
        return
    with open_file(manifest, "read") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("npm manifest is too large.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid npm project manifest.") from None
    if not isinstance(value, dict):
        raise ValueError("Expected an npm project object.")  # noqa: TRY004 -- invalid API data
    if any(value.get(k) for k in ("workspaces", "overrides", "bundledDependencies",
                                   "bundleDependencies")):
        raise PermissionDenied("Workspace/override/bundled npm dependencies need separate owner review.")
    approved = dict(package_parts("npm", p) for p in candidate.packages)
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        dependencies = value.get(field, {})
        if not isinstance(dependencies, dict):
            raise ValueError("Invalid npm dependency map.")  # noqa: TRY004 -- invalid API data
        for name, version in dependencies.items():
            if not isinstance(version, str) or approved.get(name) != version:
                raise PermissionDenied("Include every existing exact npm direct dependency in the approved list.")


def prepare_directories(root: Path, manager: str) -> None:
    """Only private task directories; Linux ownership matches Docker's fixed UID."""
    names = [".tmp", ".home"]
    if manager == "pip":
        names += [".deps", ".deps/python"]
    for name in names:
        path = root / name
        if not path.exists():
            path.mkdir(mode=0o700)
        directory(path)
        if os.name == "posix" and path.stat().st_uid != 65534:
            if os.geteuid() != 0:
                raise PermissionDenied("Provision private install directories for container UID 65534 first.")
            os.chown(path, 65534, 65534)
        path.chmod(0o700)
    if manager == "npm":
        # npm rejects loading the same file as both user and global config.
        # User config is /dev/null; global config is a distinct trusted empty file.
        config = root / ".home/.npm-globalrc"
        with open_file(config, "read" if config.exists() else "create") as stream:
            if stream.readable() and stream.read(1):
                raise PermissionDenied("npm global configuration must be empty trusted data.")
        if os.name == "posix" and config.stat().st_uid != 65534:
            if os.geteuid() != 0:
                raise PermissionDenied("Provision private npm configuration for UID 65534 first.")
            os.chown(config, 65534, 65534)
        config.chmod(0o600)


def same_binding(actual, expected) -> None:
    if actual != expected:
        raise Conflict("Task checkpoint, snapshot or permissions changed; prepare a new plan.")


def verified_versions(result: ExecutionResult, candidate: DependencyCandidate) -> bool | None:
    """True: exact match; False: confirmed mismatch; None: unverifiable effect."""
    if (result.status != "success" or result.exit_code != 0
            or not result.cleanup_confirmed or result.output_truncated):
        return None
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return None
    expected = dict(package_parts("pip", p) for p in candidate.packages)
    if not isinstance(value, dict) or set(value) != {"versions"}:
        return None
    versions = value["versions"]
    if (not isinstance(versions, dict) or set(versions) != set(expected)
            or any(not isinstance(found, list) or any(type(v) is not str for v in found)
                   for found in versions.values())):
        return None
    return all(versions[name] == [version] for name, version in expected.items())
