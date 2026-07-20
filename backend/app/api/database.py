from fastapi import APIRouter
from sqlalchemy import text

from app.db.database import engine

router = APIRouter(tags=["Database"])


@router.get("/health/db")
async def database_health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return {
            "database": "connected",
            "status": "healthy",
        }

    except Exception as exc:
        return {
            "database": "disconnected",
            "status": "unhealthy",
            "error": str(exc),
        }
