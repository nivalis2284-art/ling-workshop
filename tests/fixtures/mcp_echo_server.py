"""A minimal MCP server over stdio, used only by the tests.

Speaks just enough of the protocol to exercise the client: ``initialize``,
``tools/list`` and ``tools/call``. The revision it answers with is taken from
``MCP_FIXTURE_VERSION`` so a mismatch can be provoked deliberately.
"""

import json
import os
import sys

VERSION = os.environ.get("MCP_FIXTURE_VERSION", "2025-06-18")

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "explode",
        "description": "Always reports a tool error.",
        "inputSchema": {"type": "object"},
    },
]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        identifier = message.get("id")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "result": {
                        "protocolVersion": VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": identifier, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "result": {
                            "content": [
                                {"type": "text", "text": "echo:" + str(arguments.get("text", ""))}
                            ],
                            "isError": False,
                        },
                    }
                )
            elif name == "explode":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "result": {
                            "content": [{"type": "text", "text": "boom"}],
                            "isError": True,
                        },
                    }
                )
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "error": {"code": -32602, "message": "unknown tool"},
                    }
                )
        elif identifier is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )


if __name__ == "__main__":
    main()
