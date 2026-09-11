from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.tools import ToolInfo
from app.tools.builtin import build_default_registry

router = APIRouter(
    prefix="/tools",
    tags=["Tools"],
)


@router.get(
    "",
    response_model=list[ToolInfo],
)
def list_tools(
    db: Session = Depends(get_db),
) -> list[ToolInfo]:
    registry = build_default_registry(db)

    return [
        ToolInfo(name=tool.name, description=tool.description)
        for tool in registry.list_tools()
    ]
