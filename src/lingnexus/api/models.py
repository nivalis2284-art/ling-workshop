from fastapi import APIRouter, Depends, Request
from pydantic import SecretStr

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner
from lingnexus.models import ModelProfile

router = APIRouter(prefix="/models", dependencies=[Depends(require_owner)])


class ProfileInput(Input):
    name: str
    profile: ModelProfile
    api_key: SecretStr | None = None
    profile_id: str | None = None


class ModelSelection(Input):
    primary: str
    fallback: str | None = None


@router.get("")
def profiles(request: Request):
    return request.app.state.services.profiles.list()


@router.post("", status_code=201)
def save(value: ProfileInput, request: Request):
    return request.app.state.services.profiles.save(
        value.name,
        value.profile,
        api_key=value.api_key.get_secret_value() if value.api_key is not None else None,
        profile_id=value.profile_id,
    )


@router.get("/selection")
def selected(request: Request):
    return request.app.state.services.profiles.selection()


@router.put("/selection")
def select(value: ModelSelection, request: Request):
    return request.app.state.services.profiles.select(value.primary, value.fallback)
