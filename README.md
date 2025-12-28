# GhostNet Daemon (Overworld Nexus)

GhostNet Daemon is a FastAPI-backed service and Discord-integrated assistant designed for the **Overworld Nexus** universe. It supports chat, world document ingestion, and retrieval-augmented generation (RAG) while enforcing strict in-universe canon boundaries and safety/anti-hallucination rules.

> **Lore rule:** Discord chat is considered *in-universe* (“canon”). Real-world (“OOC/IRL”) responses should only happen when explicitly requested.

---

## Features

- **Discord Assistant**: Responds in-server with lore-aware behavior.
- **FastAPI API**: Clean HTTP interface for chat, ingest, and search endpoints.
- **RAG / World Memory**:
  - Upsert world documents (lore, factions, locations, rules).
  - Semantic search for relevant canon context.
- **Postgres-ready persistence** (migration in progress / supported).
- **Canon Guardrails**:
  - Anti-hallucination posture: prefer “unknown” over fabricated answers.
  - Refuses non-canon security-bypass / unauthorized access requests (even in-universe).
  - 18+ setting assumptions (no minors introduced into scenes).

---

## Architecture (High Level)

- **API Service**: FastAPI app (e.g., `api/app.py`)
- **DB Layer**: SQLAlchemy sessions + migrations (Postgres recommended)
- **RAG Layer**: Embeddings + similarity search + document upsert
- **Clients**: Shared HTTP clients (e.g., `httpx`) and dependency injection
- **Discord Bot**: Connects to API for chat + world search responses

