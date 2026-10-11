"""Clearable Windows user-encrypted cache, separate from portable business data."""

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

from lingnexus.tools._paths import is_link

MAX_CACHE_BYTES = 1024 * 1024


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("密钥缓存需要 Windows 当前用户加密。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately no LOCAL_MACHINE flag.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("无法读取或保存当前 Windows 用户的模型密钥缓存。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def _plain(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if is_link(info):
            raise OSError("密钥缓存路径不能使用链接或重解析点。")


class CredentialCache:
    def __init__(self, path: Path):
        self.path = path.absolute()
        self.error: str | None = None

    def load(self) -> dict[str, tuple[str, str]]:
        try:
            _plain(self.path)
            if not self.path.exists():
                return {}
            if not self.path.is_file() or self.path.stat().st_size > MAX_CACHE_BYTES:
                raise ValueError
            value = json.loads(_crypt(self.path.read_bytes(), decrypt=True))
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "credentials"}
                or value["version"] != 1
            ):
                raise ValueError
            entries = value["credentials"]
            if not isinstance(entries, dict) or len(entries) > 1024:
                raise ValueError
            result = {}
            for pid, item in entries.items():
                if not isinstance(pid, str) or not isinstance(item, list) or len(item) != 2:
                    raise ValueError
                endpoint, key = item
                if not isinstance(endpoint, str) or not isinstance(key, str) or not key:
                    raise ValueError
                result[pid] = (endpoint, key)
            self.error = None
            return result
        except (OSError, ValueError, TypeError, UnicodeError):
            self.error = "本地密钥缓存无法读取，请清除缓存后重新填写。"
            return {}

    def save(self, entries: dict[str, tuple[str, str]]) -> None:
        _plain(self.path)
        if not entries:
            self.clear()
            return
        encoded = json.dumps({"version": 1, "credentials": entries}, ensure_ascii=False).encode()
        if len(encoded) > MAX_CACHE_BYTES // 2:
            raise ValueError("密钥缓存超过容量限制。")
        encrypted = _crypt(encoded)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _plain(self.path)
        fd, name = tempfile.mkstemp(prefix="credentials-", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            _plain(self.path)
            os.replace(temporary, self.path)
            self.error = None
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        _plain(self.path)
        self.path.unlink(missing_ok=True)
        self.error = None
