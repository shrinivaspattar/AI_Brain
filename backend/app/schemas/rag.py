from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=50)


class AncestryStep(BaseModel):
    kind: str
    path: str


class SourceOccurrence(BaseModel):
    """One physical observed occurrence of a Chain 2 result's content
    identity - never "the" source, never ranked by authority (Milestone
    22 design). `archive_ancestry` is populated only for a genuinely
    nested (more than one archive level) occurrence."""

    root_t7_path: str
    member_path: str | None
    archive_ancestry: list[AncestryStep] | None


class SearchResult(BaseModel):
    chunk_id: int
    document_id: str
    document_title: str
    document_source: str
    chunk_index: int
    content: str
    score: float
    # None for Chain 1 results (no SourceInstance graph exists);
    # a SourceInstance.id-ordered list of every observed occurrence for
    # a Chain 2 result, unfiltered by canonical status.
    source_occurrences: list[SourceOccurrence] | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
