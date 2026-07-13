from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.version import router as version_router

app = FastAPI(
    title="AI_Brain",
    description="Personal AI Knowledge System",
    version="0.1.0",
)


@app.get("/")
async def root():
    return {
        "message": "Welcome to AI_Brain"
    }


app.include_router(health_router)
app.include_router(version_router)
