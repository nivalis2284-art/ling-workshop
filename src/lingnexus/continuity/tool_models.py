"""Strict tool parameters: calling run owns all identity and authorization."""
from pydantic import BaseModel, ConfigDict, Field


class BranchStart(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    objective:str=Field(min_length=1,max_length=8000)
    request_id:str=Field(min_length=1,max_length=200)
    profile_id:str|None=None


class BranchStatus(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    branch_id:str|None=None


class BranchGuide(BranchStatus):
    text:str=Field(min_length=1,max_length=8000)
    request_id:str=Field(min_length=1,max_length=200)


class BranchState(BranchStatus):
    decisions:list[str]|None=Field(default=None,max_length=64)
    progress:list[str]|None=Field(default=None,max_length=64)
    unfinished:list[str]|None=Field(default=None,max_length=64)
    verified:list[str]|None=Field(default=None,max_length=64)
    unknowns:list[str]|None=Field(default=None,max_length=64)
    artifacts:list[str]|None=Field(default=None,max_length=64)
    sources:list[str]|None=Field(default=None,max_length=64)


class ExperienceRecords(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    experience_id:str=Field(min_length=1,max_length=200)
    cursor:str|None=Field(default=None,max_length=4096)
    limit:int=Field(default=20,ge=1,le=20)


class ExperienceRead(ExperienceRecords):
    source_id:str=Field(min_length=1,max_length=200)
    max_bytes:int=Field(default=8192,ge=256,le=16384)
