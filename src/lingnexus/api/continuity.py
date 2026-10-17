"""Owner-authenticated continuity endpoints, also mounted under /api."""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from lingnexus.api.schemas import Input
from lingnexus.api.security import require_owner

router=APIRouter(dependencies=[Depends(require_owner)])


class MainInput(Input):
    conversation_id:str|None=Field(default=None,min_length=1,max_length=200)
    expected_revision:int=Field(ge=0)


class WorkInput(Input):
    request_id:str=Field(min_length=1,max_length=200)
    objective:str=Field(min_length=1,max_length=8000)
    profile_id:str|None=Field(default=None,min_length=1,max_length=200)
    origin_message_id:str|None=Field(default=None,min_length=1,max_length=200)


class GuideInput(Input):
    request_id:str=Field(min_length=1,max_length=200)
    text:str=Field(min_length=1,max_length=8000)


class RevisionInput(Input):
    expected_revision:int=Field(ge=1)


class CompactionInput(Input):
    expected_revision:int=Field(ge=0)
    enabled:bool=False
    profile_modes:dict[str,Literal['disabled','safe_epoch_restart','native_compaction']]=Field(default_factory=dict)
    trigger_ratio:float=Field(default=.8,gt=.1,lt=1)
    target_ratio:float=Field(default=.5,gt=.05,lt=1)
    max_summary_bytes:int=Field(default=16384,ge=1024,le=65536)
    max_epochs:int=Field(default=16,ge=1,le=32)


def service(request):
    return request.app.state.services.continuity


@router.get('/agents/{agent_id}/main-conversation')
def main(agent_id:str,request:Request):
    return service(request).main(agent_id)


@router.put('/agents/{agent_id}/main-conversation')
def bind_main(agent_id:str,value:MainInput,request:Request):
    return service(request).bind_main(agent_id,**value.model_dump())


@router.get('/agents/{agent_id}/work-branches')
def work_list(agent_id:str,request:Request):
    return service(request).list_work(agent_id)


@router.post('/agents/{agent_id}/work-branches',status_code=201)
def work_start(agent_id:str,value:WorkInput,request:Request):
    return service(request).branch(agent_id,**value.model_dump())


@router.get('/work-branches/{branch_id}')
def work_get(branch_id:str,request:Request):
    return service(request).work(branch_id)


@router.get('/work-branches/{branch_id}/task-state')
def task_state(branch_id:str,request:Request):
    return service(request).task_state(branch_id)


@router.get('/work-branches/{branch_id}/task-states')
def task_states(branch_id:str,request:Request,before:str|None=Query(None,max_length=4096),
                limit:int=Query(100,ge=1,le=100)):
    return service(request).task_states(branch_id,before=before,limit=limit)


@router.get('/work-branches/{branch_id}/episodes')
def episodes(branch_id:str,request:Request,before:str|None=Query(None,max_length=4096),
             limit:int=Query(100,ge=1,le=100)):
    return service(request).episodes(branch_id,before=before,limit=limit)


@router.get('/work-branches/{branch_id}/archives')
def archives(branch_id:str,request:Request,before:str|None=Query(None,max_length=4096),
             limit:int=Query(100,ge=1,le=100)):
    return service(request).archives(branch_id,before=before,limit=limit)


@router.get('/work-branches/{branch_id}/guidance')
def guidance(branch_id:str,request:Request,before:str|None=Query(None,max_length=4096),
             limit:int=Query(100,ge=1,le=100)):
    return service(request).guidance(branch_id,before=before,limit=limit)


@router.post('/work-branches/{branch_id}/guide')
def guide(branch_id:str,value:GuideInput,request:Request):
    return service(request).guide(branch_id,**value.model_dump())


@router.post('/work-branches/{branch_id}/cancel')
async def cancel(branch_id:str,value:RevisionInput,request:Request):
    item=service(request).cancel(branch_id,**value.model_dump())
    await request.app.state.services.branch_coordinator.tick()
    return service(request).work(item['id'])


@router.post('/work-branches/{branch_id}/finish')
async def finish(branch_id:str,value:RevisionInput,request:Request):
    item=service(request).request_finish(branch_id,**value.model_dump())
    await request.app.state.services.branch_coordinator.tick()
    return service(request).work(item['id'])


@router.post('/work-branches/{branch_id}/wake')
async def wake(branch_id:str,value:RevisionInput,request:Request):
    item=service(request).wake(branch_id,**value.model_dump())
    await request.app.state.services.branch_coordinator.tick()
    return service(request).work(item['id'])


@router.get('/agents/{agent_id}/experiences')
def experiences(agent_id:str,request:Request,kind:str|None=None,
                cursor:str|None=Query(None,max_length=4096),limit:int=Query(20,ge=1,le=20)):
    return service(request).list_experiences(agent_id,kind=kind,cursor=cursor,limit=limit)


@router.get('/experiences/{experience_id}/records')
def records(experience_id:str,request:Request,conversation_id:str|None=None,
            cursor:str|None=Query(None,max_length=4096),limit:int=Query(20,ge=1,le=20)):
    return service(request).experience_records(experience_id,conversation_id=conversation_id,cursor=cursor,limit=limit)


@router.get('/experiences/{experience_id}/records/{source_id}')
def read(experience_id:str,source_id:str,request:Request,conversation_id:str|None=None,
         cursor:str|None=Query(None,max_length=4096),max_bytes:int=Query(8192,ge=4,le=16384)):
    return service(request).read_experience_record(experience_id,source_id,conversation_id=conversation_id,cursor=cursor,max_bytes=max_bytes)


@router.post('/experiences/{experience_id}/revoke')
def revoke(experience_id:str,value:RevisionInput,request:Request):
    c=service(request)
    item=c.experience(experience_id)
    return c.revoke_experience(experience_id,agent_id=item['agent_id'],**value.model_dump())


@router.get('/agents/{agent_id}/compaction-settings')
def compaction_settings(agent_id:str,request:Request):
    runtime = request.app.state.chat_runtime
    return runtime.compaction.settings(agent_id)


@router.put('/agents/{agent_id}/compaction-settings')
def configure_compaction(agent_id:str,value:CompactionInput,request:Request):
    runtime = request.app.state.chat_runtime
    return runtime.compaction.configure(agent_id,**value.model_dump())
