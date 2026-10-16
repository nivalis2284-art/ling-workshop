"""Owner-authenticated browser routes; parent mounts router and sets state.browser."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field

from lingnexus.api.input_invalidation import cancel_inputs
from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.browser import ActionInput, BrowserService, OriginGrant
from lingnexus.common.errors import Conflict, DomainError, NotFound, PermissionDenied


class BrowserRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422, content={"detail": "Invalid browser request fields"}
                )
            except DomainError as error:
                status = (
                    409
                    if isinstance(error, Conflict)
                    else 404
                    if isinstance(error, NotFound)
                    else 403
                    if isinstance(error, PermissionDenied)
                    else 422
                )
                return JSONResponse(status_code=status, content={"detail": str(error)})
            except ValueError:
                return JSONResponse(status_code=422, content={"detail": "Invalid browser request"})

        return safe_handler


router = APIRouter(dependencies=[Depends(require_owner)], route_class=BrowserRoute)


def service(request: Request) -> BrowserService:
    browser = getattr(request.app.state, "browser", None)
    if browser is None:
        raise HTTPException(503, "Browser service is not initialized by the parent lifespan")
    return browser


class OriginInput(Input):
    expected_revision: int = Field(ge=0)
    grants: list[OriginGrant] = Field(max_length=100)


class ApprovalInput(Input):
    decision: Literal["approve", "reject"]
    expected_intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LoginInput(Input):
    url: str = Field(min_length=1, max_length=8192)
    origin_revision: int = Field(ge=1)
    headed: Literal[True]


@router.get("/agents/{agent_id}/browser/origins")
async def origins(agent_id: str, request: Request):
    return service(request).current_origins(agent_id)


@router.put("/agents/{agent_id}/browser/origins")
async def configure(agent_id: str, value: OriginInput, request: Request):
    result = await service(request).configure_origins(
        agent_id, value.grants, expected_revision=value.expected_revision
    )
    await cancel_inputs(request, agent_id=agent_id)
    return result


@router.get("/agents/{agent_id}/browser/state")
async def state(agent_id: str, request: Request):
    return await service(request).state(agent_id)


@router.post("/agents/{agent_id}/browser/actions")
async def submit(agent_id: str, value: ActionInput, request: Request):
    return await service(request).submit(agent_id, value)


@router.get("/agents/{agent_id}/browser/actions/{action_id}")
async def action(agent_id: str, action_id: str, request: Request):
    return service(request).get_action(agent_id, action_id)


@router.get("/agents/{agent_id}/browser/approvals")
async def pending(agent_id: str, request: Request):
    return service(request).pending_approvals(agent_id)


@router.post("/agents/{agent_id}/browser/approvals/{approval_id}/decision")
async def decide(agent_id: str, approval_id: str, value: ApprovalInput, request: Request):
    return await service(request).decide(
        approval_id,
        agent_id=agent_id,
        decision=value.decision,
        expected_intent_hash=value.expected_intent_hash,
    )


@router.post("/agents/{agent_id}/browser/login")
async def login(agent_id: str, value: LoginInput, request: Request):
    return await service(request).open_login(agent_id, **value.model_dump())


@router.delete("/agents/{agent_id}/browser/login")
async def finish_login(agent_id: str, request: Request):
    return await service(request).finish_login(agent_id)
