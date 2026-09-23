from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# AI_Brain project root
BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    PROJECT_NAME: str = "AI_Brain"
    VERSION: str = "0.1.0"

    DATABASE_URL: str = ""
    INGESTION_DIR: Path = BASE_DIR / "documents" / "imports"
    CHAT_UPLOADS_DIR: Path = BASE_DIR / "documents" / "chat_uploads"
    REDIS_URL: str = "redis://localhost:6379"

    # Comma-separated absolute paths of the read-only master backup(s) (see
    # docs/decisions/0002). The dedup filesystem executor refuses to operate
    # on, inside, or above any of them.
    MASTER_BACKUP_PATHS: str = ""

    OLLAMA_HOST: str = "http://localhost:11434"
    CHAT_MODEL: str = "qwen3:8b"
    # Models a person can pick per-conversation in the chat UI, in addition
    # to CHAT_MODEL (always included as the default/first choice even if not
    # listed here). Comma-separated; each must already be pulled in Ollama -
    # this list is just an allowlist of names, it does not pull anything.
    AVAILABLE_CHAT_MODELS: str = "qwen3:8b,qwen3:4b,dolphin-mistral"

    # Files dropped directly into a chat turn (not the T7 ingestion pipeline -
    # see app/models/chat_attachment.py). Provisional caps, not calibrated
    # against real prompt-budget testing on this hardware.
    MAX_CHAT_ATTACHMENT_BYTES: int = 20_000_000
    MAX_CHAT_ATTACHMENT_TEXT_CHARS: int = 20_000
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

    # Web search: the one place this project reaches the live internet, so
    # it is off by default at two layers - this global switch, and a
    # per-message toggle in the UI (ChatRequest.web_search). Both must be
    # true for a search to happen. Backed by a self-hosted SearXNG (see
    # docker-compose.yml / docker/searxng), not a third-party search API,
    # so a query never leaves machines this project controls.
    WEB_SEARCH_ENABLED: bool = False
    SEARXNG_URL: str = "http://localhost:8080"
    WEB_SEARCH_MAX_RESULTS: int = 5

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        case_sensitive=True,
    )

    def master_backup_paths(self) -> list[Path]:
        return [Path(p.strip()) for p in self.MASTER_BACKUP_PATHS.split(",") if p.strip()]

    def available_chat_models(self) -> list[str]:
        """CHAT_MODEL first (it is always offered, even if someone edits
        AVAILABLE_CHAT_MODELS without it), then the rest in the order given,
        with duplicates dropped."""
        names = [self.CHAT_MODEL] + [m.strip() for m in self.AVAILABLE_CHAT_MODELS.split(",") if m.strip()]
        seen: list[str] = []
        for name in names:
            if name not in seen:
                seen.append(name)
        return seen


settings = Settings()
