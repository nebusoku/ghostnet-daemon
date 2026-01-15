# api/settings.py
from pydantic import BaseModel
import os


class Settings(BaseModel):
    # Auth
    api_key: str = os.getenv("API_KEY", "change-me")

    # Database (single source of truth)
    # Default is sqlite for dev fallback, but production should always set DATABASE_URL.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./ghostnet.db")

    # Model + vector config
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    chat_model: str = os.getenv("CHAT_MODEL", "llama3.2:1b")
    embed_model: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")

    # Note: you currently have both `collection` and `qdrant_collection`.
    # Keep both for backward compatibility; we'll clean up later.
    collection: str = os.getenv("QDRANT_COLLECTION", "ghostnet_docs")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "ghostnet_docs")

    # RAG tuning
    max_input_tokens: int = int(os.getenv("MAX_INPUT_TOKENS", 6000))
    top_k: int = int(os.getenv("RETRIEVAL_TOP_K", 2))
    chunk_size: int = int(os.getenv("CHUNK_SIZE", 1000))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", 150))


settings = Settings()
