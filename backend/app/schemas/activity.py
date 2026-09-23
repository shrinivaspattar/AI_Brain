from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.chat import ConversationSummary


class RecentAttachment(BaseModel):
    id: str
    filename: str
    byte_size: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RecentIngestionBatch(BaseModel):
    id: int
    status: str
    source_instances_selected: int
    created_at: datetime
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class RecentActivityResponse(BaseModel):
    """Deliberately plain recency views over data that already exists -
    no LLM summaries, no new knowledge architecture. See docs/roadmap
    notes on why this stays SQL-only maintenance, not a milestone."""

    recent_conversations: list[ConversationSummary]
    recent_attachments: list[RecentAttachment]
    recent_ingestion_batches: list[RecentIngestionBatch]
