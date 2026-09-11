from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ImportJobCreate(BaseModel):
    name: str
    source_path: str
    source_type: str


class ImportJobResponse(BaseModel):
    id: int
    name: str
    source_path: str
    source_type: str
    status: str
    progress: float
    files_discovered: int
    files_processed: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
