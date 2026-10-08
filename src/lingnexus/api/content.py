from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from lingnexus.api.security import require_owner
from lingnexus.content.service import MAX_FILE_BYTES

router = APIRouter(dependencies=[Depends(require_owner)])


@router.post("/agents/{agent_id}/attachments", status_code=201)
async def upload(agent_id: str, request: Request, file: UploadFile):
    try:
        data = await file.read(MAX_FILE_BYTES + 1)
        return await run_in_threadpool(
            request.app.state.services.attachments.add, agent_id, file.filename or "", data
        )
    finally:
        await file.close()


@router.get("/agents/{agent_id}/attachments")
def list_attachments(agent_id: str, request: Request, limit: int = 100, offset: int = 0):
    return request.app.state.services.attachments.list(agent_id, limit=limit, offset=offset)


@router.get("/attachments/{attachment_id}")
def read_attachment(attachment_id: str, request: Request):
    return request.app.state.services.attachments.read(attachment_id)


@router.get("/attachments/{attachment_id}/download")
def download(attachment_id: str, request: Request):
    data = request.app.state.services.attachments.read_bytes(attachment_id)
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="attachment"',
            "X-Content-Type-Options": "nosniff",
        },
    )
