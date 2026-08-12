from fastapi import FastAPI
from app.api.database import router as database_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.version import router as version_router
from app.core.config import settings
from app.api.import_jobs import router as import_jobs_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Personal AI Knowledge System",
    version=settings.VERSION,
)


@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.PROJECT_NAME}"}


app.include_router(health_router)
app.include_router(version_router)
app.include_router(database_router)
app.include_router(documents_router)
app.include_router(import_jobs_router)
