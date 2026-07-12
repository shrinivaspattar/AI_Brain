from fastapi import FastAPI
from datetime import datetime

app = FastAPI(
    title="AI_Brain",
    version="0.1.0",
    description="Personal Offline AI Assistant"
)

START_TIME = datetime.now()


@app.get("/")
async def root():
    return {
        "name": "AI_Brain",
        "version": "0.1.0",
        "status": "running"
    }


@app.get("/health")
async def health():
    uptime = datetime.now() - START_TIME

    return {
        "status": "healthy",
        "uptime": str(uptime).split(".")[0]
    }
