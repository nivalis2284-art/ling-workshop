"""Versioned evidence shared by chat and task runs; never a semantic grader."""

import hashlib
import json
from pathlib import Path

MUTATIONS = frozenset({"create_file", "write_file", "edit_file"})
_CODE = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs", ".cpp", ".c"}
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
         ".ruff_cache", ".tmp", ".home", ".deps", ".artifacts"}


def workspace_version(root: Path | None) -> str | None:
    """Bounded content identity, excluding execution scratch and dependency trees."""
    if root is None:
        return None
    try:
        from lingnexus.tools._paths import directory, regular_file, reject_link
        from lingnexus.tools.workspace import FilePermissions, WorkspaceTools

        root = directory(root)
        access = WorkspaceTools(root, FilePermissions((root,), ()))
        digest = hashlib.sha256()
        pending, count, size = [root], 0, 0
        while pending:
            folder = pending.pop()
            for path in sorted(folder.iterdir()):
                if path.name in _SKIP or path.name.startswith(".env"):
                    continue
                info = path.lstat()
                reject_link(info)
                if path.is_dir():
                    pending.append(directory(path))
                else:
                    regular_file(info)
                    data = access.read_bytes(path)
                    count += 1
                    size += len(data)
                    if count > 2000 or size > 32 * 1_048_576:
                        return None
                    digest.update(path.relative_to(root).as_posix().encode())
                    digest.update(b"\0" + hashlib.sha256(data).digest())
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def successful(name: str, result: dict) -> bool:
    if name in {"run_command", "command_status"}:
        return bool(result.get("status") == "success" and result.get("exit_code") == 0
                    and result.get("cleanup_confirmed") and not result.get("timed_out")
                    and not result.get("output_truncated") and not result.get("artifact_error")
                    and not result.get("error") and not result.get("version_unavailable"))
    return result.get("status") == "completed"


def initial(*, coding: bool = False) -> dict:
    return {"failures": {}, "unknown_failures": {}, "failure_history": [], "writes": {},
            "checked": {}, "verified": False, "command_verified": False,
            "command_used": False, "coding_workspace": coding, "revision": 0,
            "verification": {"status": "unverified", "kind": None, "checks": []}}


def invalidate(state: dict, *, unknown: bool = False) -> None:
    old = state.get("verification", {})
    state["revision"] = state.get("revision", 0) + 1
    state.update(verified=False, command_verified=False)
    state["verification"] = {**old, "status": "unknown" if unknown else "stale",
                             "revision": state["revision"]}


def account(state: dict, call: dict, result: dict) -> None:
    """Record actual effects, preserving unknown effects across later test success."""
    for key, value in initial().items():
        state.setdefault(key, value)
    name, args = call["name"], call["arguments"]
    key = name + ":" + str(args.get("path", json.dumps(args.get("argv", args), sort_keys=True)))
    okay = successful(name, result)
    pending = result.get("status") in {"running", "cancelling"}
    unknown = result.get("status") == "unknown" or (
        name in {"run_command", "command_status"} and result.get("cleanup_confirmed") is False
    )
    if not okay and not pending:
        code = result.get("error_code") or result.get("error") or "tool_failed"
        state["failures"][key] = code
        state["failure_history"] = [*state["failure_history"], {"tool": name, "code": code}][-32:]
        if unknown:
            state["unknown_failures"][key] = code
    elif okay and key not in state["unknown_failures"]:
        state["failures"].pop(key, None)

    if name in MUTATIONS:
        # Even a failed/unknown write must not retain an old green result.
        invalidate(state, unknown=unknown)
        if okay:
            path = args["path"]
            content = args.get("text")
            state["writes"][path] = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
            state["checked"].pop(path, None)
            if name == "edit_file" or Path(path).suffix.lower() in _CODE:
                state["coding_workspace"] = True
    elif name == "read_file" and okay:
        text = result.get("result", {}).get("text", "")
        state["checked"][args["path"]] = hashlib.sha256(text.encode()).hexdigest()
        # Non-code artifacts can be verified by exact readback; code cannot.
        if state["writes"] and not state["coding_workspace"] and not state["command_used"]:
            state["verified"] = all(state["checked"].get(p) == h for p, h in state["writes"].items())
            if state["verified"]:
                state["verification"] = {"status": "passed", "kind": "content", "checks": [],
                                         "revision": state["revision"]}
    elif name in {"run_command", "command_status"}:
        state["command_used"] = True
        invalidate(state, unknown=unknown)
        purpose = result.get("execution_purpose", args.get("purpose"))
        if okay and purpose == "verify":
            state["command_verified"] = True
            state["verified"] = not state["unknown_failures"]
            # A new successful check supersedes known exploratory failures only.
            state["failures"] = dict(state["unknown_failures"])
            checks = state["verification"].get("checks", [])
            state["verification"] = {
                "status": "unknown" if state["unknown_failures"] else "passed",
                "kind": "command", "revision": state["revision"],
                "workspace_version": result.get("workspace_version"),
                "checks": [*checks, {"argv": args.get("argv", result.get("checked_argv", [])),
                                      "exit_code": 0}][-8:],
            }
        elif not pending:
            state["verification"]["status"] = "unknown" if unknown else "failed" if not okay else "stale"

    if name.startswith("browser_") and name != "browser_state":
        state["browser_used"] = True
        state["browser_verified"] = bool(okay and name == "browser_read" and result.get("verification_passed"))
    if state.get("browser_used"):
        required = state["command_used"] or (state["coding_workspace"] and state["writes"])
        state["verified"] = bool(state.get("browser_verified") and
                                 (state["command_verified"] if required else not state["writes"] or state["verified"]))
    if state["unknown_failures"]:
        state.update(verified=False, command_verified=False)
        state["verification"]["status"] = "unknown"


def ensure_current(state: dict, root: Path | None) -> None:
    evidence = state.get("verification", {})
    if evidence.get("status") != "passed" or evidence.get("kind") != "command" or root is None:
        return
    expected = evidence.get("workspace_version")
    actual = workspace_version(root)
    if expected is None or actual is None or actual != expected:
        invalidate(state, unknown=actual is None)


def public(state: dict) -> dict:
    value = state.get("verification", {})
    return {key: value[key] for key in ("status", "kind", "revision", "checks") if key in value}
