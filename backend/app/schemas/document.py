from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentCreate(BaseModel):
    title: str
    source: str
    source_type: str
    import_job_id: int | None = None


class DocumentResponse(BaseModel):
    id: str
    title: str
    source: str
    source_type: str
    import_job_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
