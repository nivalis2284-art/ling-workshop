from fastapi import APIRouter, Depends, Request

from lingnexus.api.security import require_owner
from lingnexus.common.errors import NotFound
from lingnexus.observability.usage import UsageService

router = APIRouter(dependencies=[Depends(require_owner)])


@router.get("/agents/{agent_id}/usage")
def usage(agent_id: str, request: Request):
    database = request.app.state.services.database
    with database.transaction() as db:
        if db.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
            raise NotFound("个体不存在")
    return UsageService(database).summary(agent_id)
