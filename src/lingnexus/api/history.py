from fastapi import APIRouter, Depends, Request

from lingnexus.api.schemas import EpisodeCreate, SourceCorrection, SourceCreate
from lingnexus.api.security import require_owner
from lingnexus.common.errors import PermissionDenied
from lingnexus.common.types import SourceRef

router = APIRouter(dependencies=[Depends(require_owner)])


@router.post("/sources", status_code=201)
def record(value: SourceCreate, request: Request):
    return request.app.state.services.history.add(
        value.agent_id, value.content, kind="user", scope="real", request_id=value.request_id
    )


@router.get("/sources/{source_id}")
def read(source_id: str, revision: int, request: Request):
    return request.app.state.services.history.read(SourceRef(id=source_id, revision=revision))


@router.patch("/sources/{source_id}")
def correct(source_id: str, value: SourceCorrection, request: Request):
    ref = SourceRef(id=source_id, revision=value.revision)
    record = request.app.state.services.history.read(ref)
    if record["kind"] != "user":
        raise PermissionDenied("只能修改自己的消息；对灵的回复可重新生成或纠正派生认识")
    return request.app.state.services.history.correct(
        SourceRef(id=source_id, revision=value.revision), value.content
    )


@router.post("/episodes", status_code=201)
def save_episode(value: EpisodeCreate, request: Request):
    return {
        "id": request.app.state.services.memory.save_episode(
            value.agent_id, value.title, value.summary, value.sources, request_id=value.request_id
        )
    }


@router.get("/episodes/{episode_id}")
def read_episode(episode_id: str, request: Request):
    return request.app.state.services.memory.read_episode(episode_id)


@router.get("/agents/{agent_id}/episodes")
def search(agent_id: str, request: Request, query: str = "", limit: int = 20, offset: int = 0):
    return request.app.state.services.memory.find_episodes(
        agent_id, query, limit=limit, offset=offset
    )
