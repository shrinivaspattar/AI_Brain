from fastapi import APIRouter

router = APIRouter(tags=["Version"])


@router.get("/version")
async def version():
    return {
        "application": "AI_Brain",
        "version": "0.1.0",
    }
