from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api.chat import router as chat_router
from app.api.database import router as database_router
from app.api.dedup import router as dedup_router
from app.api.dedup_execution_plans import router as dedup_execution_plans_router
from app.api.dedup_plan_authorizations import router as dedup_plan_authorizations_router
from app.api.dedup_reviews import router as dedup_reviews_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.memory import router as memory_router
from app.api.rag import router as rag_router
from app.api.tools import router as tools_router
from app.api.version import router as version_router
from app.core.config import BASE_DIR, settings
from app.api.import_jobs import router as import_jobs_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Personal AI Knowledge System",
    version=settings.VERSION,
)


app.include_router(health_router)
app.include_router(version_router)
app.include_router(database_router)
app.include_router(documents_router)
app.include_router(import_jobs_router)
app.include_router(rag_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(dedup_router)
app.include_router(dedup_reviews_router)
app.include_router(dedup_execution_plans_router)
app.include_router(dedup_plan_authorizations_router)

# Mounted last so it never shadows an API route above: Starlette checks
# routes in registration order, and a Mount at "/" only gets a chance to
# match once every earlier, more specific route has already missed.
app.mount(
    "/",
    StaticFiles(directory=BASE_DIR / "frontend", html=True),
    name="frontend",
)
