FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The app locates frontend/ relative to the repo root (BASE_DIR in
# app/core/config.py is three levels above config.py), so the image keeps
# the same layout: /app/backend and /app/frontend.
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/documents/imports \
    && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/backend
EXPOSE 8000

# DATABASE_URL must be provided at runtime (the app builds its engine at
# import time and refuses an empty URL). Ollama is reached via OLLAMA_HOST.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
