from pydantic import BaseModel
import os
class Settings(BaseModel):
    api_key: str = os.getenv("API_KEY", "change-me")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    chat_model: str = os.getenv("CHAT_MODEL", "llama3.2:1b")
    embed_model: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    collection: str = os.getenv("QDRANT_COLLECTION", "ghostnet_docs")
    max_input_tokens: int = int(os.getenv("MAX_INPUT_TOKENS", 6000))
    top_k: int = int(os.getenv("RETRIEVAL_TOP_K", 2))
    chunk_size: int = int(os.getenv("CHUNK_SIZE", 1000))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", 150))
settings = Settings()
