"""Reject unauthorized/oversized uploads before FastAPI's multipart spooling."""

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lingnexus.api.security import require_owner
from lingnexus.content.service import MAX_FILE_BYTES


class UploadBoundary:
    def __init__(self, app: ASGIApp):
        self.app = app
        self.limit = MAX_FILE_BYTES + 64 * 1024  # multipart headers/framing allowance

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not path.endswith("/attachments")
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            require_owner(request)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(
                scope, receive, send
            )
            return
        length = request.headers.get("content-length")
        if length:
            try:
                size = int(length)
            except ValueError:
                size = -1
            if size < 0 or size > self.limit:
                await JSONResponse({"detail": "附件请求超过允许大小"}, status_code=413)(
                    scope, receive, send
                )
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise HTTPException(413, "附件请求超过允许大小")
            return message

        await self.app(scope, limited_receive, send)
