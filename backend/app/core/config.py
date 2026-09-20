from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# AI_Brain project root
BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    PROJECT_NAME: str = "AI_Brain"
    VERSION: str = "0.1.0"

    DATABASE_URL: str = ""
    INGESTION_DIR: Path = BASE_DIR / "documents" / "imports"
    REDIS_URL: str = "redis://localhost:6379"

    OLLAMA_HOST: str = "http://localhost:11434"
    CHAT_MODEL: str = "qwen3:8b"
    # Qwen3 "thinking" adds hidden reasoning tokens before every reply. On
    # this project's CPU-only dev machine that made a simple tool-call turn
    # take 145 s instead of 6.5 s, so it is off by default. Set
    # CHAT_THINKING_ENABLED=true to trade speed for more deliberate answers.
    CHAT_THINKING_ENABLED: bool = False
    EMBEDDING_MODEL: str = "nomic-embed-text"
    EMBEDDING_DIMENSIONS: int = 768

    # Query-embedding cache in Redis. Off by default: nothing changes until
    # it is enabled. The TTL is a plain default, not a tuned value.
    EMBEDDING_CACHE_ENABLED: bool = False
    EMBEDDING_CACHE_TTL_SECONDS: int = 86400

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        case_sensitive=True,
    )


settings = Settings()
