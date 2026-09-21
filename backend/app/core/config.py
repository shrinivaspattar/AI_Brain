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

    # Comma-separated absolute paths of the read-only master backup(s) (see
    # docs/decisions/0002). The dedup filesystem executor refuses to operate
    # on, inside, or above any of them.
    MASTER_BACKUP_PATHS: str = ""

    OLLAMA_HOST: str = "http://localhost:11434"
    CHAT_MODEL: str = "qwen3:8b"
    # Qwen3 "thinking" adds hidden reasoning tokens before every reply. On
    # this project's CPU-only dev machine that made a simple tool-call turn
    # take 145 s instead of 6.5 s, so it is off by default. Set
    # CHAT_THINKING_ENABLED=true to trade speed for more deliberate answers.
    CHAT_THINKING_ENABLED: bool = False
    # Sources farther than this cosine distance from the question are neither
    # shown nor given to the model. None = keep all (the calibrated value is
    # set in .env, see the offline-chat evaluation).
    CHAT_MAX_SOURCE_DISTANCE: float | None = None
    EMBEDDING_MODEL: str = "nomic-embed-text"
    EMBEDDING_DIMENSIONS: int = 768

    # Query-embedding cache in Redis. Off by default: nothing changes until
    # it is enabled. The TTL is a plain default, not a tuned value.
    # Hybrid search: fuse dense (pgvector) and BM25 rankings with RRF. Off by
    # default - the measured gain (Experiment 001) is on public technical docs,
    # not on personal notes. The pool is how many chunks each ranking
    # contributes before fusion; a plain default, not tuned.
    SEARCH_HYBRID_ENABLED: bool = False
    SEARCH_HYBRID_POOL: int = 50

    EMBEDDING_CACHE_ENABLED: bool = False
    EMBEDDING_CACHE_TTL_SECONDS: int = 86400

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        case_sensitive=True,
    )

    def master_backup_paths(self) -> list[Path]:
        return [Path(p.strip()) for p in self.MASTER_BACKUP_PATHS.split(",") if p.strip()]


settings = Settings()
