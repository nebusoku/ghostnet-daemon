# api/settings.py
from pydantic import BaseModel
import os


class Settings(BaseModel):
    # Auth
    api_key: str = os.getenv("API_KEY", "change-me")

    # Database (single source of truth)
    # Default is sqlite for dev fallback, but production should always set DATABASE_URL.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./ghostnet.db")

    # --- Generation backend -------------------------------------------------
    # "ollama" (local) or "openai" (any OpenAI-compatible /v1 endpoint).
    # Generation and embedding are configured separately on purpose: local
    # embedding is fast (~2.5s single forward pass), local generation is not
    # (measured 0.31 tok/s on a 1B => ~37 tokens per 120s timeout).
    llm_backend: str = os.getenv("LLM_BACKEND", "ollama")

    # OpenAI-compatible settings. No default model id: it must match whatever
    # the configured provider actually serves, so a wrong guess fails loudly
    # at call time rather than silently 404ing.
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "")

    # Shared generation tuning (applies to whichever backend is selected)
    # gen_timeout is sized for SLOW LOCAL generation: a canon-sized prompt
    # costs ~57s of prompt evaluation on this host before the first token.
    gen_timeout: int = int(os.getenv("GEN_TIMEOUT", 120))

    # Hosted providers get a much tighter per-link bound. With a deep model
    # stack, gen_timeout applied per link would let one hung provider stall
    # the chain for minutes. Rate-limited links return in well under a second,
    # so even a 20-deep chain of 429s costs only a few seconds before the
    # local floor is reached.
    hosted_timeout: int = int(os.getenv("HOSTED_TIMEOUT", 60))

    max_output_tokens: int = int(os.getenv("MAX_OUTPUT_TOKENS", 512))
    num_ctx: int = int(os.getenv("NUM_CTX", 8192))

    # Ollama thread count. Measured optimum on this VM is 8 (6-12 all fine);
    # 24 and Ollama's own default are both ~30x slower. Do not remove or raise
    # to the core count -- see the measurement table in api/llm.py first.
    num_thread: int = int(os.getenv("NUM_THREAD", 8))

    # Embeddings always run on Ollama (see api/rag.py)
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    chat_model: str = os.getenv("CHAT_MODEL", "llama3.2:1b")
    embed_model: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")

    # Note: you currently have both `collection` and `qdrant_collection`.
    # Keep both for backward compatibility; we'll clean up later.
    collection: str = os.getenv("QDRANT_COLLECTION", "ghostnet_docs")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "ghostnet_docs")

    # --- Provider failover ---------------------------------------------------
    # Ordered chain, tried left to right until one answers. Put the local
    # backend last: it always responds, so it is a good floor, but it is the
    # slowest and weakest and should never be reached first.
    llm_chain: str = os.getenv("LLM_CHAIN", "")

    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL",
                                         "https://openrouter.ai/api/v1")
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "")
    # Ordered fallback list. Individual free models get rate-limited UPSTREAM
    # (the provider behind them, not your account) -- observed as
    # "google/gemma-4-31b-it:free is temporarily rate-limited upstream" while
    # the account still had 49 of 50 daily requests left. Stacking models and
    # falling through turns a transient 429 into a retry on another model.
    # Comma-separated; falls back to OPENROUTER_MODEL when unset.
    openrouter_models: str = os.getenv("OPENROUTER_MODELS", "")
    # Optional attribution headers OpenRouter uses for ranking.
    openrouter_referer: str = os.getenv("OPENROUTER_REFERER", "")
    openrouter_title: str = os.getenv("OPENROUTER_TITLE", "")

    # --- Memory --------------------------------------------------------------
    # Prompt cost must stay flat as conversations grow. See api/memory.py.
    memory_enabled: bool = os.getenv("MEMORY_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "y", "on"
    )
    # Token budget for conversation history (scene memory).
    memory_scene_tokens: int = int(os.getenv("MEMORY_SCENE_TOKENS", 1500))
    # Token budget for player dossier + events + absence digest.
    memory_player_tokens: int = int(os.getenv("MEMORY_PLAYER_TOKENS", 600))
    memory_absence_tokens: int = int(os.getenv("MEMORY_ABSENCE_TOKENS", 250))

    # Compaction thresholds.
    memory_max_turns: int = int(os.getenv("MEMORY_MAX_TURNS", 40))
    memory_compact_after: int = int(os.getenv("MEMORY_COMPACT_AFTER", 20))
    memory_keep_verbatim: int = int(os.getenv("MEMORY_KEEP_VERBATIM", 6))
    memory_summary_tokens: int = int(os.getenv("MEMORY_SUMMARY_TOKENS", 300))

    # Selection caps for player-scoped memory.
    memory_max_player_events: int = int(os.getenv("MEMORY_MAX_PLAYER_EVENTS", 12))
    memory_max_world_events: int = int(os.getenv("MEMORY_MAX_WORLD_EVENTS", 8))
    # How long away before a returning player gets an absence digest (24h).
    memory_absence_seconds: int = int(os.getenv("MEMORY_ABSENCE_SECONDS", 86400))

    # Summarising is a slow-path job with nobody waiting, so it runs locally
    # and costs nothing at the paid provider. That is the point.
    memory_compact_backend: str = os.getenv("MEMORY_COMPACT_BACKEND", "ollama")

    # Chars-per-token for budget estimation. Rough on purpose -- see
    # api/memory.py:estimate_tokens for why there is no tokeniser dependency.
    token_chars_per: int = int(os.getenv("TOKEN_CHARS_PER", 4))

    # nomic-embed-text requires task prefixes; disable only if you switch to
    # an embedding model that does not use them.
    embed_use_prefix: bool = os.getenv("EMBED_USE_PREFIX", "true").strip().lower() in (
        "1", "true", "yes", "y", "on"
    )

    # RAG tuning
    max_input_tokens: int = int(os.getenv("MAX_INPUT_TOKENS", 6000))

    # Retrieval depth. Was 2, which injected two documents out of a twelve
    # document corpus -- asked about factions the model saw two of the three
    # and invented five more. 6 fits comfortably now that num_ctx is 8192.
    top_k: int = int(os.getenv("RETRIEVAL_TOP_K", 6))

    # Score floor for a hit to count as canon. Calibrated against
    # nomic-embed-text WITH task prefixes, where real canon scores 0.55-0.70
    # and unrelated text sits near 0.45. Retune if the embedding model changes.
    rag_score_threshold: float = float(os.getenv("RAG_SCORE_THRESHOLD", 0.55))
    rag_max_docs: int = int(os.getenv("RAG_MAX_DOCS", 6))

    # Character budget for retrieved context. Was hardcoded at 1200 (~300
    # tokens) when num_ctx was 2048; at 8192 that cap threw away most of what
    # retrieval found.
    rag_context_chars: int = int(os.getenv("RAG_CONTEXT_CHARS", 4000))
    chunk_size: int = int(os.getenv("CHUNK_SIZE", 1000))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", 150))


settings = Settings()
