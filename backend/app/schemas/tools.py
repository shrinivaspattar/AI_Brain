from pydantic import BaseModel


class ToolInfo(BaseModel):
    name: str
    description: str
