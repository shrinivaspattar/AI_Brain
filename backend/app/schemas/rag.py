from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=50)


class SearchResult(BaseModel):
    chunk_id: int
    document_id: str
    document_title: str
    document_source: str
    chunk_index: int
    content: str
    score: float


class SearchResponse(BaseModel):
    results: list[SearchResult]
