from unittest.mock import MagicMock

import pytest

from app.memory.service import MemoryService
from app.models.memory import Memory
from app.schemas.memory import MemoryCreate


def test_create_memory_persists_memory() -> None:
    db = MagicMock()
    service = MemoryService(db)

    data = MemoryCreate(content="User prefers dark mode.")

    result = service.create_memory(data)

    assert isinstance(result, Memory)
    assert result.content == "User prefers dark mode."
    assert result.confidence is None
    assert result.conversation_id is None
    assert result.message_id is None

    db.add.assert_called_once_with(result)
    db.commit.assert_called_once_with()
    db.refresh.assert_called_once_with(result)
    db.rollback.assert_not_called()


def test_create_memory_persists_optional_fields() -> None:
    db = MagicMock()
    service = MemoryService(db)

    data = MemoryCreate(
        content="User's name is Shrinivas.",
        confidence=0.9,
        conversation_id="conv-1",
        message_id=5,
    )

    result = service.create_memory(data)

    assert result.confidence == 0.9
    assert result.conversation_id == "conv-1"
    assert result.message_id == 5


def test_create_memory_rolls_back_on_commit_failure() -> None:
    db = MagicMock()
    db.commit.side_effect = RuntimeError("database failure")

    service = MemoryService(db)

    with pytest.raises(RuntimeError, match="database failure"):
        service.create_memory(MemoryCreate(content="test"))

    db.rollback.assert_called_once_with()


def test_get_memory_returns_none_when_missing() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = MemoryService(db)

    assert service.get_memory(42) is None


def test_list_memories_returns_scalars() -> None:
    db = MagicMock()
    memories = [MagicMock(), MagicMock()]
    db.scalars.return_value = memories

    service = MemoryService(db)

    result = service.list_memories()

    assert result == memories


def test_delete_memory_removes_existing_memory() -> None:
    db = MagicMock()
    memory = Memory(id=1, content="test")
    db.get.return_value = memory

    service = MemoryService(db)
    service.delete_memory(1)

    db.delete.assert_called_once_with(memory)
    db.commit.assert_called_once_with()


def test_delete_memory_raises_for_missing_memory() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = MemoryService(db)

    with pytest.raises(ValueError, match="Memory 42 not found"):
        service.delete_memory(42)

    db.delete.assert_not_called()
