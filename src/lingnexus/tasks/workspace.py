"""Bounded safe snapshots and explicit owner review; no automatic host writeback."""

import difflib
import hashlib
import os
from pathlib import Path

from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.tools import FilePermissions, WorkspaceTools
from lingnexus.tools._paths import directory, local_path, regular_file, reject_link

EXCLUDED = {".git", "venv", ".venv", "node_modules", "__pycache__", ".tmp", ".deps", ".home"}
MAX_FILE = 1_048_576
MAX_TOTAL = 32 * 1_048_576
MAX_FILES = 2000


def excluded(path):
    return any(p.lower() in EXCLUDED or p.lower().startswith(".env") for p in Path(path).parts)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tools(root):
    return WorkspaceTools(
        root, FilePermissions((root,), (root,)), max_read_bytes=MAX_FILE, max_write_bytes=MAX_FILE
    )


def authorize(app, agent, root):
    root = directory(root)
    grants = app.permissions.current(agent)
    if not all(
        any(root.is_relative_to(directory(p)) for p in grants[k]) for k in ("readable", "writable")
    ):
        raise PermissionDenied("A project workspace requires both read and write grants.")
    return root


def scan(root):
    """Excluded trees are never opened; included paths use handle-based safe reads."""
    root = directory(root)
    files, total, entries = {}, 0, 0
    access = tools(root)
    pending = [root]
    while pending:
        folder = directory(pending.pop())
        with os.scandir(folder) as children:
            for entry in children:
                entries += 1
                if entries > 10_000:
                    raise ValueError("Snapshot entry limit exceeded.")
                path = local_path(entry.path)
                relative = path.relative_to(root)
                if excluded(relative):
                    continue
                info = path.lstat()
                reject_link(info)
                if entry.is_dir(follow_symlinks=False):
                    if path.is_mount():
                        raise PermissionDenied("Nested mounts cannot be snapshotted.")
                    pending.append(directory(path))
                else:
                    regular_file(info)
                    data = access.read_bytes(path)
                    total += len(data)
                    if total > MAX_TOTAL or len(files) >= MAX_FILES:
                        raise ValueError("Snapshot size limit exceeded.")
                    files[relative.as_posix()] = data
    return files


def make_parents(root, relative):
    parent = root
    directory(root)
    for part in local_path(relative).parent.parts:
        parent /= part
        if not parent.exists():
            parent.mkdir(mode=0o777)
            parent.chmod(0o777)
        directory(parent)


class Snapshots:
    def __init__(self, app, store):
        self.app, self.store = app, store
        self.root = app.database.path.absolute().parent / "task-data"

    def path(self, tid):
        # Task IDs come exclusively from existing rows, never from model arguments.
        self.store.get(tid)
        return directory(self.root / tid / "workspace")

    def create(self, tid, agent, source):
        source = authorize(self.app, agent, source)
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise PermissionDenied("Project workspace must be separate from task data.")
        files = scan(source)
        self.root.mkdir(exist_ok=True)
        directory(self.root)
        task_dir = self.root / tid
        task_dir.mkdir()
        for name in ("workspace", "baseline"):
            root = task_dir / name
            root.mkdir(mode=0o777)
            root.chmod(0o777)
            for relative, data in files.items():
                make_parents(root, relative)
                tools(root).create_bytes(relative, data)
                (root / relative).chmod(0o666)
        scratch = task_dir / "workspace" / ".tmp"
        scratch.mkdir(mode=0o777)
        scratch.chmod(0o777)
        return {p: digest(b) for p, b in files.items()}

    def file_path(self, tid, raw):
        root = self.path(tid)
        task = self.store.get(tid)
        if raw.startswith("/workspace/"):
            candidate = local_path(raw[len("/workspace/") :])
        else:
            candidate = local_path(raw)
            if candidate.is_absolute():
                source = Path(task["workspace"])
                if not candidate.is_relative_to(source):
                    raise PermissionDenied("File is outside the task workspace.")
                candidate = candidate.relative_to(source)
        if candidate.is_absolute() or excluded(candidate):
            raise PermissionDenied("File is excluded from the task snapshot.")
        return root / candidate

    def changes(self, tid):
        task = self.store.get(tid)
        if not task["workspace"]:
            return {"review_id": None, "files": []}
        if task["status"] in {"running", "queued"}:
            raise Conflict("Pause the task before reviewing changes.")
        current = scan(self.path(tid))
        baseline = self.store.load(tid)["baseline"]
        hashes = {p: digest(b) for p, b in current.items()}
        manifest, files = [], []
        base_tools = tools(directory(self.root / tid / "baseline"))
        for p in sorted(baseline.keys() | hashes.keys()):
            if baseline.get(p) == hashes.get(p):
                continue
            old = base_tools.read_bytes(p) if p in baseline else b""
            new = current.get(p, b"")
            diff = None
            try:
                diff = "".join(
                    difflib.unified_diff(
                        old.decode("utf-8").splitlines(keepends=True),
                        new.decode("utf-8").splitlines(keepends=True),
                        fromfile="a/" + p,
                        tofile="b/" + p,
                    )
                )[:65_536]
            except UnicodeError:
                pass
            change = {
                "path": p,
                "before": baseline.get(p),
                "after": hashes.get(p),
                "operation": "delete"
                if p not in hashes
                else "modify"
                if p in baseline
                else "create",
            }
            manifest.append(change)
            files.append({**change, "diff": diff, "artifact": p if p in hashes else None})
        rid = self.store.review(tid, manifest)
        return {"review_id": rid, "files": files}

    def artifact(self, tid, path):
        target = self.file_path(tid, path)
        return tools(self.path(tid)).read_bytes(target)

    def apply(self, tid, rid):
        task = self.store.get(tid)
        if task["status"] in {"running", "queued"} or not task["workspace"]:
            raise Conflict("Task changes are not ready for application.")
        review = self.store.get_review(tid, rid)
        if review["status"] == "applied":
            return review["result"]
        if review["status"] != "reviewed":
            raise Conflict("Interrupted application needs owner reconciliation.")
        with self.app.database.transaction():
            source = authorize(self.app, task["agent_id"], task["workspace"])
            baseline = self.store.load(tid)["baseline"]
            if {p: digest(b) for p, b in scan(source).items()} != baseline:
                raise Conflict("Original workspace changed since the task snapshot.")
            current = scan(self.path(tid))
            hashes = {p: digest(b) for p, b in current.items()}
            expected = {c["path"]: c["after"] for c in review["manifest"] if c["after"] is not None}
            actual = {p: h for p, h in hashes.items() if baseline.get(p) != h}
            deletions = baseline.keys() - hashes.keys()
            if actual != expected or deletions != {
                c["path"] for c in review["manifest"] if c["after"] is None
            }:
                raise Conflict("Staged artifacts changed after review.")
            # Existing action helpers cannot safely delete or atomically replace binary
            # files. Such artifacts remain downloadable; never invent a host operation.
            if deletions:
                raise Conflict("Deletions require manual application of the reviewed diff.")
            try:
                texts = {p: current[p].decode("utf-8") for p in expected}
            except UnicodeError:
                raise Conflict("Binary artifacts require manual application.") from None
        # Fence commits BEFORE filesystem writes. Unknown/partial applications never replay.
        self.store.review_status(rid, "applying")
        results = []
        try:
            for relative, text in texts.items():
                with self.app.database.transaction():
                    authorize(self.app, task["agent_id"], source)
                    target = source / relative
                    existing = tools(source).read_bytes(target) if target.exists() else None
                    if (digest(existing) if existing is not None else None) != baseline.get(
                        relative
                    ):
                        raise Conflict("An application target changed.")
                    make_parents(source, relative)
                    name = "write_file" if relative in baseline else "create_file"
                    result = self.app.actions.execute(
                        task["agent_id"],
                        "task-apply:" + rid + ":" + relative,
                        name,
                        {"path": str(target), "text": text},
                    )
                    results.append(result["id"])
                    if result["status"] != "completed":
                        raise Conflict("Application effect requires owner review.")
            outcome = {"status": "applied", "review_id": rid, "effect_ids": results}
            self.store.review_status(rid, "applied", outcome)
            self.store.event(tid, "applied", outcome)
            return outcome
        except Exception:  # noqa: BLE001 -- fence every partial filesystem application
            self.store.review_status(rid, "unknown", {"effect_ids": results})
            raise Conflict(
                "Application stopped; reconcile the recorded effects before continuing."
            ) from None
