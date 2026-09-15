from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class AncestryStep(BaseModel):
    kind: str
    path: str


class SourceOccurrence(BaseModel):
    """One physical observed occurrence of a Chain 2 citation's content
    identity - never "the" source, never ranked by authority (Milestone
    22 design). `archive_ancestry` is populated only for a genuinely
    nested (more than one archive level) occurrence."""

    root_t7_path: str
    member_path: str | None
    archive_ancestry: list[AncestryStep] | None


class Citation(BaseModel):
    document_chunk_id: int
    document_id: str
    document_title: str
    document_source: str
    # None for Chain 1 citations (no SourceInstance graph exists);
    # a SourceInstance.id-ordered list of every observed occurrence for
    # a Chain 2 citation, unfiltered by canonical status.
    # Default None so a Citation stored before this field existed
    # (Message.citations is a plain JSONB list of dicts, never
    # migrated) still validates - a genuinely missing key means the
    # same thing an explicit null does: no provenance recorded.
    source_occurrences: list[SourceOccurrence] | None = None


class MessageResponse(BaseModel):
    id: int
    conversation_id: str
    role: str
    content: str
    citations: list[Citation] | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatResponse(BaseModel):
    conversation_id: str
    message: MessageResponse
