import secrets

from fastapi import HTTPException, Request


def require_owner(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in request.app.state.allowed_origins:
        raise HTTPException(403, "请求来源未获允许")
    value = request.headers.get("authorization", "")
    expected = "Bearer " + request.app.state.owner_token
    if not secrets.compare_digest(value.encode(), expected.encode()):
        raise HTTPException(401, "请通过本机启动入口打开界面")
