from pydantic import BaseModel, ConfigDict, Field


class FilePath(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)


class FileWrite(FilePath):
    text: str


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500)


class EpisodeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    episode_id: str


class SourceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    revision: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=10000)


SCHEMAS = {
    "read_file": FilePath,
    "create_file": FileWrite,
    "write_file": FileWrite,
    "find_memories": MemoryQuery,
    "read_episode": EpisodeRead,
    "read_source": SourceRead,
    "find_knowledge": MemoryQuery,
}


def descriptors() -> list[dict]:
    descriptions = {
        "read_file": "读取所有者授权目录内的 UTF-8 文件。",
        "create_file": "在授权工作目录创建普通文件；不覆盖已有文件。",
        "write_file": "修改授权工作目录内的已有普通文件。",
        "find_memories": "查找当前有效事件摘要，必要时继续读取来源。",
        "read_episode": "按事件编号读取当前有效摘要和原始来源引用。",
        "read_source": "按确切来源编号和修订读取原始经历的片段，可以继续分页。",
        "find_knowledge": "查找已有知识、经验和报告，返回其来源，不能把报告当成确定事实。",
    }
    return [
        {"name": name, "description": descriptions[name], "parameters": model.model_json_schema()}
        for name, model in SCHEMAS.items()
    ]
