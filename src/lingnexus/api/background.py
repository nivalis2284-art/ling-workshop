from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router = APIRouter(prefix="/background", dependencies=[Depends(require_owner)])


class BackgroundInput(Input):
    enabled: bool


@router.get("")
def status(request: Request):
    return request.app.state.background.status()


@router.put("")
async def configure(value: BackgroundInput, request: Request):
    return request.app.state.background.configure(enabled=value.enabled)


@router.post("/run-once")
async def run_once(request: Request):
    return await request.app.state.background.run_now()


@router.get("/jobs")
def jobs(request: Request, limit: int = 100):
    limit = max(1, min(limit, 100))
    with request.app.state.services.database.transaction() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT j.id,j.agent_id,j.kind,j.status,j.created_at,s.attempts,s.outcome,s.error_code FROM jobs j LEFT JOIN learning_job_state s ON s.job_id=j.id ORDER BY j.created_at DESC LIMIT ?",
                (limit,),
            )
        ]


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, request: Request):
    return request.app.state.learning.retry(job_id)
