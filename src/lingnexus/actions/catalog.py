from pydantic import BaseModel, ConfigDict, Field


class FilePath(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)


class FileWrite(FilePath):
    text: str


SCHEMAS = {
    "read_file": FilePath,
    "create_file": FileWrite,
    "write_file": FileWrite,
}


def descriptors() -> list[dict]:
    descriptions = {
        "read_file": "读取所有者授权目录内的 UTF-8 文件。",
        "create_file": "在授权工作目录创建普通文件；不覆盖已有文件。",
        "write_file": "修改授权工作目录内的已有普通文件。",
    }
    return [
        {"name": name, "description": descriptions[name], "parameters": model.model_json_schema()}
        for name, model in SCHEMAS.items()
    ]
