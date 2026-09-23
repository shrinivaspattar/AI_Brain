from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.chat_attachment import ChatAttachment
from app.models.conversation import Conversation
from app.models.ingestion_batch import IngestionBatch
from app.schemas.activity import RecentActivityResponse, RecentAttachment, RecentIngestionBatch
from app.schemas.chat import ConversationSummary

router = APIRouter(prefix="/activity", tags=["Activity"])

RECENT_LIMIT = 5


@router.get("/recent", response_model=RecentActivityResponse)
def recent_activity(db: Session = Depends(get_db)) -> RecentActivityResponse:
    """Plain recency views over conversations, chat attachments, and
    ingestion batches - SQL only, no LLM involved. See RecentActivityResponse
    for why this stays a maintenance/UI feature, not a new milestone."""
    conversations = list(
        db.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(RECENT_LIMIT))
    )
    attachments = list(
        db.scalars(select(ChatAttachment).order_by(ChatAttachment.created_at.desc()).limit(RECENT_LIMIT))
    )
    batches = list(
        db.scalars(select(IngestionBatch).order_by(IngestionBatch.created_at.desc()).limit(RECENT_LIMIT))
    )

    return RecentActivityResponse(
        recent_conversations=[ConversationSummary.model_validate(c) for c in conversations],
        recent_attachments=[
            RecentAttachment(id=a.id, filename=a.original_filename, byte_size=a.byte_size, created_at=a.created_at)
            for a in attachments
        ],
        recent_ingestion_batches=[RecentIngestionBatch.model_validate(b) for b in batches],
    )
