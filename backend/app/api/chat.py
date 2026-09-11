from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.chat import ChatRequest, ChatResponse, MessageResponse
from app.services.chat_client import ChatUnavailableError
from app.services.chat_service import ChatService

router = APIRouter(
    prefix="/chat",
    tags=["Chat"],
)


@router.post(
    "",
    response_model=ChatResponse,
    responses={
        404: {"description": "Conversation not found"},
        503: {"description": "Chat model unavailable"},
    },
)
def send_message(
    request: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    service = ChatService(db)

    try:
        message = service.send_message(
            request.message,
            conversation_id=request.conversation_id,
            top_k=request.top_k,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except ChatUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Chat model unavailable: {exc}",
        )

    return ChatResponse(
        conversation_id=message.conversation_id,
        message=MessageResponse.model_validate(message),
    )


@router.get(
    "/{conversation_id}",
    response_model=list[MessageResponse],
    responses={404: {"description": "Conversation not found"}},
)
def get_conversation_messages(
    conversation_id: str,
    db: Session = Depends(get_db),
) -> list[MessageResponse]:
    conversation = db.get(Conversation, conversation_id)

    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {conversation_id} not found",
        )

    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    messages = list(db.scalars(statement))

    return [MessageResponse.model_validate(message) for message in messages]
