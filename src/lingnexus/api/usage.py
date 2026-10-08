from fastapi import APIRouter, Depends, Request

from lingnexus.api.security import require_owner
from lingnexus.observability.usage import UsageService

router = APIRouter(dependencies=[Depends(require_owner)])


@router.get("/agents/{agent_id}/usage")
def usage(agent_id: str, request: Request):
    request.app.state.services.identity.current(agent_id)
    return UsageService(request.app.state.services.database).summary(agent_id)
