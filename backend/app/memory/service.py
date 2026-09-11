from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory import Memory
from app.schemas.memory import MemoryCreate

DEFAULT_LIST_LIMIT = 100


class MemoryService:
    def __init__(self, db: Session):
        self.db = db

    def create_memory(
        self,
        data: MemoryCreate,
    ) -> Memory:
        memory = Memory(
            content=data.content,
            confidence=data.confidence,
            conversation_id=data.conversation_id,
            message_id=data.message_id,
        )

        try:
            self.db.add(memory)
            self.db.commit()
            self.db.refresh(memory)
            return memory

        except Exception:
            self.db.rollback()
            raise

    def get_memory(
        self,
        memory_id: int,
    ) -> Memory | None:
        return self.db.get(Memory, memory_id)

    def list_memories(
        self,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[Memory]:
        statement = (
            select(Memory)
            .order_by(Memory.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement))

    def delete_memory(
        self,
        memory_id: int,
    ) -> None:
        memory = self.get_memory(memory_id)

        if memory is None:
            raise ValueError(f"Memory {memory_id} not found")

        try:
            self.db.delete(memory)
            self.db.commit()

        except Exception:
            self.db.rollback()
            raise
