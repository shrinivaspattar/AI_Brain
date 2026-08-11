from pydantic import BaseModel


class DocumentCreate(BaseModel):
    title: str
    source: str
    source_type: str
