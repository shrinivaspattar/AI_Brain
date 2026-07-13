from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["Version"])


@router.get("/version")
async def version():
    db_url = settings.DATABASE_URL

    if "://" in db_url and "@" in db_url:
        scheme, rest = db_url.split("://", 1)
        creds, host = rest.split("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            db_url = f"{scheme}://{user}:********@{host}"

    return {
        "application": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "database_url": db_url,
    }