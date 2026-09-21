import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.embeddings.client import EmbeddingUnavailableError
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
        503: {"description": "Chat model or embedding model unavailable"},
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
    except EmbeddingUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Embedding model unavailable: {exc}",
        )

    return ChatResponse(
        conversation_id=message.conversation_id,
        message=MessageResponse.model_validate(message),
    )


@router.post(
    "/stream",
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}},
)
def stream_message(
    request: ChatRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Same as POST /chat, but streamed as server-sent events so the reply
    appears while it is being written. Events (one JSON object per `data:`
    line): {"type": "token", "text"}, {"type": "reset"}, one final
    {"type": "done", "conversation_id", "message"}, or {"type": "error",
    "detail"} if the turn could not finish."""
    service = ChatService(db)

    def event_lines():
        try:
            for event in service.send_message_stream(
                request.message,
                conversation_id=request.conversation_id,
                top_k=request.top_k,
            ):
                if event["type"] == "done":
                    message = event["message"]
                    event = {
                        "type": "done",
                        "conversation_id": message.conversation_id,
                        "message": MessageResponse.model_validate(message).model_dump(mode="json"),
                    }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield _error_event(str(exc))
        except ChatUnavailableError as exc:
            yield _error_event(f"Chat model unavailable: {exc}")
        except EmbeddingUnavailableError as exc:
            yield _error_event(f"Embedding model unavailable: {exc}")

    return StreamingResponse(
        event_lines(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _error_event(detail: str) -> str:
    return f"data: {json.dumps({'type': 'error', 'detail': detail}, ensure_ascii=False)}\n\n"


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
