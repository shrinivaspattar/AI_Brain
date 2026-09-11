from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.rag.retrieval_service import RetrievalService
from app.schemas.rag import SearchRequest, SearchResponse, SearchResult

router = APIRouter(
    prefix="/rag",
    tags=["RAG"],
)


@router.post(
    "/search",
    response_model=SearchResponse,
)
def search(
    request: SearchRequest,
    db: Session = Depends(get_db),
) -> SearchResponse:
    service = RetrievalService(db)
    results = service.search(request.query, top_k=request.top_k)

    return SearchResponse(
        results=[
            SearchResult(
                chunk_id=result.chunk.id,
                document_id=result.document.id,
                document_title=result.document.title,
                document_source=result.document.source,
                chunk_index=result.chunk.chunk_index,
                content=result.chunk.content,
                score=1 - result.distance,
            )
            for result in results
        ]
    )
