"""One MCP server connection over either transport.

Both transports are reduced to a single ``exchange`` call so the JSON-RPC layer
above them has no idea which one it is talking to:

* **stdio** — spawn the server, write newline-delimited JSON, read replies on a
  background thread (``select`` cannot watch pipes on Windows, and a blocking
  read would hang shutdown);
* **http** — POST the message with ``urllib`` and accept either a plain JSON
  reply or an event-stream frame.

Nothing here decides permissions. A remote tool call is dispatched by the same
dispatcher, through the same hooks, as any local tool.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import urllib.error
import urllib.request

from .protocol import (
    CLIENT_INFO,
    PROTOCOL_VERSION,
    McpError,
    decode,
    encode,
    error_from,
    negotiate,
    notification,
    request,
)

_TOOL_LIMIT = 500
_PAGE_LIMIT = 20


class StdioTransport:
    """A server run as a local child process, speaking newline-delimited JSON."""

    def __init__(self, command: str, args: list[str], env: dict | None, cwd: str | None):
        self._command = command
        self._args = args
        self._env = env
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._closed = False

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(
                [self._command, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # A chatty server must not fill a pipe nobody drains and deadlock.
                stderr=subprocess.DEVNULL,
                env=self._env,
                cwd=self._cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise McpError("mcp_server_unavailable", f"无法启动服务器：{exc}") from exc
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        stream = self._process.stdout if self._process else None
        try:
            if stream is not None:
                for line in stream:
                    self._queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)

    def exchange(self, message: dict, *, expect_reply: bool, timeout: float) -> dict | None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError("mcp_server_unavailable", "服务器进程未启动")
        try:
            process.stdin.write(encode(message))
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError("mcp_connection_closed", "服务器已关闭输入流") from exc
        if not expect_reply:
            return None
        identifier = message.get("id")
        while True:
            try:
                line = self._queue.get(timeout=timeout)
            except queue.Empty as exc:
                # The request may well have been executed; the caller decides.
                raise McpError("mcp_timeout", "等待服务器响应超时") from exc
            if line is None:
                raise McpError("mcp_connection_closed", "服务器已关闭连接")
            if not line.strip():
                continue
            reply = decode(line)
            if reply.get("id") == identifier:
                return reply
            # Server-initiated messages are not part of the minimal set; we
            # declared no capabilities that would invite one, so skip and wait.

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process, self._process = self._process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


class HttpTransport:
    """A remote server reached over streamable HTTP."""

    def __init__(self, url: str, headers: dict[str, str]):
        self._url = url
        self._headers = headers
        self._session: str | None = None

    def start(self) -> None:
        return None

    def exchange(self, message: dict, *, expect_reply: bool, timeout: float) -> dict | None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        call = urllib.request.Request(self._url, data=body, method="POST")
        call.add_header("content-type", "application/json")
        call.add_header("accept", "application/json, text/event-stream")
        if self._session:
            call.add_header("mcp-session-id", self._session)
        for key, value in self._headers.items():
            call.add_header(key, value)
        try:
            with urllib.request.urlopen(call, timeout=timeout) as response:
                session = response.headers.get("mcp-session-id")
                if session:
                    self._session = session
                raw = response.read()
                content_type = (response.headers.get("content-type") or "").lower()
        except urllib.error.HTTPError as exc:
            raise McpError("mcp_server_error", f"服务器返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise McpError("mcp_server_unavailable", f"无法连接服务器：{exc}") from exc
        if not expect_reply:
            return None
        return _parse_body(raw, content_type)

    def close(self) -> None:
        # Each call opens and closes its own connection, so there is nothing to
        # release here — the method exists so both transports look alike.
        return None


def _parse_body(raw: bytes, content_type: str) -> dict:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise McpError("mcp_invalid_message", "服务器返回空响应")
    if "text/event-stream" in content_type:
        # Frames are `data: {...}` lines; the first JSON object is the reply.
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload:
                return decode(payload)
        raise McpError("mcp_invalid_message", "事件流中没有数据帧")
    return decode(text)


class McpConnection:
    """Handshake, tool discovery and tool calls against one server."""

    def __init__(self, transport, *, timeout: float = 15.0):
        self._transport = transport
        self._timeout = timeout
        self._next_id = 0
        self.protocol_version = ""
        self.server_info: dict = {}
        self.tools: list[dict] = []

    def start(self) -> None:
        self._transport.start()
        result = self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        # Refuse a revision we cannot speak instead of guessing at the shape.
        self.protocol_version = negotiate(result.get("protocolVersion"))
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        self._notify("notifications/initialized")
        self.tools = self._list_tools()

    def call_tool(self, tool: str, arguments: dict) -> dict:
        return self._call("tools/call", {"name": tool, "arguments": arguments})

    def close(self) -> None:
        self._transport.close()

    # --- JSON-RPC -----------------------------------------------------------

    def _call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        reply = self._transport.exchange(
            request(self._next_id, method, params), expect_reply=True, timeout=self._timeout
        )
        if reply is None:
            raise McpError("mcp_invalid_message", f"{method} 未收到响应")
        if "error" in reply:
            raise error_from(reply)
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str) -> None:
        self._transport.exchange(
            notification(method), expect_reply=False, timeout=self._timeout
        )

    def _list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(_PAGE_LIMIT):
            params = {"cursor": cursor} if cursor else None
            page = self._call("tools/list", params)
            entries = page.get("tools")
            if isinstance(entries, list):
                tools.extend(item for item in entries if isinstance(item, dict))
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or len(tools) >= _TOOL_LIMIT:
                break
        return tools[:_TOOL_LIMIT]
