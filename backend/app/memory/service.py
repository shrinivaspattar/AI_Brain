from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory import Memory, MemoryStatus
from app.schemas.memory import MemoryCreate

DEFAULT_LIST_LIMIT = 100


class MemoryService:
    def __init__(self, db: Session):
        self.db = db

    def create_memory(
        self,
        data: MemoryCreate,
    ) -> Memory:
        """Persist a user-authored memory (via the API). Always APPROVED -
        a human explicitly asking to remember something is already the
        confirmation step; unlike a model proposal, it doesn't need review.
        """
        memory = Memory(
            content=data.content,
            confidence=data.confidence,
            conversation_id=data.conversation_id,
            message_id=data.message_id,
            status=MemoryStatus.APPROVED,
        )

        try:
            self.db.add(memory)
            self.db.commit()
            self.db.refresh(memory)
            return memory

        except Exception:
            self.db.rollback()
            raise

    def propose_memory(
        self,
        content: str,
        confidence: float | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
    ) -> Memory:
        """Persist a model-proposed memory (via the `remember` tool).

        Always PENDING - a model's own claim of confidence isn't
        independently verified, so a proposal never takes effect until a
        human reviews it (approve_memory/reject_memory). Excluded from
        list_memories(status=APPROVED), so it can't influence chat
        context until then.
        """
        memory = Memory(
            content=content,
            confidence=confidence,
            conversation_id=conversation_id,
            message_id=message_id,
            status=MemoryStatus.PENDING,
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
        status: MemoryStatus | None = None,
    ) -> list[Memory]:
        statement = select(Memory).order_by(Memory.created_at.desc()).limit(limit)

        if status is not None:
            statement = statement.where(Memory.status == status)

        return list(self.db.scalars(statement))

    def approve_memory(
        self,
        memory_id: int,
    ) -> Memory:
        memory = self.get_memory(memory_id)

        if memory is None:
            raise ValueError(f"Memory {memory_id} not found")

        try:
            memory.status = MemoryStatus.APPROVED
            self.db.commit()
            self.db.refresh(memory)
            return memory

        except Exception:
            self.db.rollback()
            raise

    def reject_memory(
        self,
        memory_id: int,
    ) -> Memory:
        memory = self.get_memory(memory_id)

        if memory is None:
            raise ValueError(f"Memory {memory_id} not found")

        try:
            memory.status = MemoryStatus.REJECTED
            self.db.commit()
            self.db.refresh(memory)
            return memory

        except Exception:
            self.db.rollback()
            raise

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
