from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.memory.service import MemoryService
from app.schemas.memory import MemoryCreate, MemoryResponse

router = APIRouter(
    prefix="/memory",
    tags=["Memory"],
)


@router.post(
    "",
    response_model=MemoryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_memory(
    memory_data: MemoryCreate,
    db: Session = Depends(get_db),
) -> MemoryResponse:
    service = MemoryService(db)
    return service.create_memory(memory_data)


@router.get(
    "",
    response_model=list[MemoryResponse],
)
def list_memories(
    db: Session = Depends(get_db),
) -> list[MemoryResponse]:
    service = MemoryService(db)
    return service.list_memories()


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Memory not found"}},
)
def delete_memory(
    memory_id: int,
    db: Session = Depends(get_db),
) -> None:
    service = MemoryService(db)

    try:
        service.delete_memory(memory_id)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
