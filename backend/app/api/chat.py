import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.embeddings.client import EmbeddingUnavailableError
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.chat import (
    AvailableModelsResponse,
    ChatRequest,
    ChatResponse,
    ConversationSummary,
    MessageResponse,
)
from app.services.chat_client import ChatClient, ChatUnavailableError
from app.services.chat_service import ChatService

router = APIRouter(
    prefix="/chat",
    tags=["Chat"],
)


def resolve_chat_model(requested: str | None) -> str | None:
    """None means "use ChatClient's own default" (settings.CHAT_MODEL) -
    keeps behaviour unchanged for any caller that never sends `model`.
    A name outside the configured allowlist is treated the same as not
    sending one, rather than passing an arbitrary string to Ollama - the
    allowlist exists so the picker only ever offers models a person chose
    to expose, not an error path for a mistyped name."""
    if requested and requested in settings.available_chat_models():
        return requested
    return None


@router.get("/models", response_model=AvailableModelsResponse)
def list_available_models() -> AvailableModelsResponse:
    return AvailableModelsResponse(models=settings.available_chat_models(), default=settings.CHAT_MODEL)


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
    service = ChatService(db, chat_client=ChatClient(model=resolve_chat_model(request.model)))

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
    service = ChatService(db, chat_client=ChatClient(model=resolve_chat_model(request.model)))

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


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    """Most recently active first - `updated_at` is touched by ChatService on
    every completed turn, not just at creation, so this reflects real activity."""
    statement = select(Conversation).order_by(Conversation.updated_at.desc())
    conversations = list(db.scalars(statement))
    return [ConversationSummary.model_validate(c) for c in conversations]


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
