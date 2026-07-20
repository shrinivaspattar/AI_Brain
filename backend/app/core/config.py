from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# AI_Brain project root
BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    PROJECT_NAME: str = "AI_Brain"
    VERSION: str = "0.1.0"

    DATABASE_URL: str = ""
    REDIS_URL: str = "redis://localhost:6379"

    OLLAMA_HOST: str = "http://localhost:11434"
    CHAT_MODEL: str = "qwen3:8b"
    EMBEDDING_MODEL: str = "nomic-embed-text"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        case_sensitive=True,
    )


settings = Settings()
