from typing import List, Optional
import json

import httpx
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, or_
from sqlalchemy.orm import Session

from .schemas import (
    ChatRequest,
    ChatResponse,
    IngestRequest,
    SearchRequest,
    WorldDocIn,
    PlayerCreate,
    PlayerUpdate,
    PlayerOut,
)
from .deps import api_key_auth, clients
from .settings import settings
from .rag import upsert_texts, search_similar, upsert_world_documents
from .db import SessionLocal, init_db
from .models import WorldDocument, Player


# ---------------------------------------------------------
# FastAPI Setup
# ---------------------------------------------------------
app = FastAPI(title="Overworld Nexus — GhostNet Daemon API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# DB dependency
# ---------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
async def on_startup():
    init_db()


# ---------------------------------------------------------
# Health checks
# ---------------------------------------------------------
@app.get("/health")
async def health(_: None = Depends(api_key_auth)):
    return {"status": "ok"}


@app.get("/health/deep")
async def health_deep(_: None = Depends(api_key_auth)):
    """
    Deep health check: DB, Qdrant, Ollama.
    """
    # --- DB ---
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db_ok, db_error = True, None
    except Exception as e:
        db_ok, db_error = False, repr(e)

    # --- Qdrant ---
    try:
        clients.qdrant.get_collections()
        qdrant_ok, qdrant_error = True, None
    except Exception as e:
        qdrant_ok, qdrant_error = False, repr(e)

    # --- Ollama ---
    try:
        r = await clients.http.get(f"{settings.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        ollama_ok, ollama_error = True, None
    except Exception as e:
        ollama_ok, ollama_error = False, repr(e)

    components = {
        "db": {"ok": db_ok, "error": db_error},
        "qdrant": {"ok": qdrant_ok, "error": qdrant_error},
        "ollama": {"ok": ollama_ok, "error": ollama_error},
    }

    if db_ok and qdrant_ok and ollama_ok:
        overall = "ok"
    elif db_ok or qdrant_ok or ollama_ok:
        overall = "degraded"
    else:
        overall = "down"

    return {"status": overall, "components": components}


# ---------------------------------------------------------
# Chat (LLM) endpoint
# ---------------------------------------------------------
OLLAMA_OPTS = {
    "num_ctx": 2048,
    # was 64 – this was causing mid-sentence cutoffs
    "num_predict": 256,
    "temperature": 0.6,
    "repeat_penalty": 1.1,
    "num_thread": 6,
}

# RAG tuning
RAG_SCORE_THRESHOLD = 0.55  # more forgiving match threshold
MAX_RAG_DOCS = 5


async def ollama_chat(http: httpx.AsyncClient, messages: List[dict]) -> str:
    r = await http.post(
        f"{settings.ollama_url}/api/chat",
        json={
            "model": settings.chat_model,
            "messages": messages,
            "stream": False,
            "options": OLLAMA_OPTS,
        },
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()

    # Standardize chat response extraction
    if isinstance(d, dict):
        if "message" in d and "content" in d["message"]:
            return d["message"]["content"]
        if "response" in d:
            return d["response"]

    raise RuntimeError(f"Unexpected chat response: {d}")


def trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _: None = Depends(api_key_auth)):
    msgs: List[dict] = []

    # In-world, anti-hallucination system prompt
    base_policy = (
        "You are GhostNet Daemon, an embedded process inside the Overworld Nexus. "
        "You speak in-world, as system logs, mesh whispers, or operator briefings — "
        "not as a generic assistant.\n\n"
        "Stay grounded in the Overworld Nexus setting and the documents you’re given. "
        "Do NOT invent hard facts that are not supported by your context.\n\n"
        "When the archives feel thin or inconsistent, say so in-character: "
        "talk about fragmented echoes, corrupted logs, or data still syncing, and "
        "invite the operator to check back later or help seed more runs into the mesh."
    )
    msgs.append({"role": "system", "content": base_policy})

    if req.system:
        msgs.append({"role": "system", "content": req.system})

    msgs.extend([m.model_dump() for m in req.messages])

    # --- RAG flow ---
    if req.rag:
        user_text = next(
            (m.content for m in reversed(req.messages) if m.role == "user"),
            "",
        )

        try:
            hits = await search_similar(
                clients.http,
                clients.qdrant,
                user_text,
                settings.top_k,
            )
        except Exception:
            hits = []

        strong = [
            d for d, s in hits if s >= RAG_SCORE_THRESHOLD
        ][:MAX_RAG_DOCS]

        if strong:
            ctx = trim("\n\n".join(strong), 1200)
            msgs.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "Context from archived Overworld Nexus echoes follows. "
                        "Anchor your answer in this material. "
                        "If it feels off-topic or insufficient, say so explicitly "
                        "instead of fabricating details.\n\n" + ctx
                    ),
                },
            )
        else:
            msgs.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "No reliable echoes were found for this query. "
                        "Answer cautiously and in-character: explain that the logs are "
                        "thin or still syncing, and avoid making up concrete lore."
                    ),
                },
            )

    # --- LLM call ---
    try:
        content = await ollama_chat(clients.http, msgs)
    except (httpx.ReadTimeout, httpx.ConnectError) as e:
        content = f"(timeout talking to local model: {e})"

    return ChatResponse(content=content)


# ---------------------------------------------------------
# RAG / Qdrant ingest + search
# ---------------------------------------------------------
@app.post("/ingest")
async def ingest(req: IngestRequest, _: None = Depends(api_key_auth)):
    await upsert_texts(
        clients.http,
        clients.qdrant,
        req.texts,
        req.metadatas or [],
    )
    return {"added": len(req.texts)}


@app.post("/search")
async def search(req: SearchRequest, _: None = Depends(api_key_auth)):
    k = req.top_k or settings.top_k
    hits = await search_similar(
        clients.http,
        clients.qdrant,
        req.query,
        k,
    )
    return {"results": [{"text": t, "score": s} for t, s in hits]}


# ---------------------------------------------------------
# World docs: create
# ---------------------------------------------------------
@app.post("/world/docs")
async def create_world_docs(
    docs: List[WorldDocIn],
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    """
    Ingest world documents into DB + Qdrant.
    """
    db_docs: List[WorldDocument] = []

    for d in docs:
        doc = WorldDocument(
            world=d.world,
            kind=d.kind,
            title=d.title,
            body=d.body,
            tags=d.tags,
            status=d.status,
            created_by=d.created_by,
            created_from_message_id=d.created_from_message_id,
        )
        db.add(doc)
        db_docs.append(doc)

    db.flush()  # assign IDs

    await upsert_world_documents(
        http=clients.http,
        qc=clients.qdrant,
        db=db,
        docs=db_docs,
    )

    return {
        "inserted": len(db_docs),
        "ids": [doc.id for doc in db_docs],
    }


# ---------------------------------------------------------
# World docs: list
# ---------------------------------------------------------
@app.get("/world/docs")
async def list_world_docs(
    world: Optional[str] = None,
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    """
    List world documents with optional filters.
    """
    q = db.query(WorldDocument)

    if world:
        q = q.filter(WorldDocument.world == world)
    if kind:
        q = q.filter(WorldDocument.kind == kind)
    if tag:
        # tags is JSON; contains() works for simple substring presence here
        q = q.filter(WorldDocument.tags.contains(tag))

    docs = q.order_by(WorldDocument.id.desc()).limit(limit).all()

    return [
        {
            "id": d.id,
            "world": d.world,
            "kind": d.kind,
            "title": d.title,
            "tags": d.tags,
            "status": d.status,
        }
        for d in docs
    ]


# ---------------------------------------------------------
# World docs: get one
# ---------------------------------------------------------
@app.get("/world/docs/{doc_id}")
async def get_world_doc(
    doc_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    """
    Fetch a single world document by ID.
    """
    doc = db.get(WorldDocument, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="WorldDocument not found")

    return {
        "id": doc.id,
        "world": doc.world,
        "kind": doc.kind,
        "title": doc.title,
        "body": doc.body,
        "tags": doc.tags,
        "status": doc.status,
        "created_by": doc.created_by,
        "created_from_message_id": doc.created_from_message_id,
    }


# ---------------------------------------------------------
# Players: upsert / get / search
# ---------------------------------------------------------
@app.post("/players", response_model=PlayerOut)
async def upsert_player(
    player: PlayerCreate,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    """
    Create or update a Player row keyed by discord_id.
    This is the main way the bot / sync scripts register players.
    """
    existing = (
        db.query(Player)
        .filter(Player.discord_id == player.discord_id)
        .first()
    )

    aliases_json = json.dumps(player.aliases or [])

    if existing:
        existing.primary_handle = player.primary_handle
        existing.display_name = player.display_name
        existing.avatar_url = player.avatar_url
        existing.is_npc = player.is_npc
        existing.aliases = aliases_json
        db.add(existing)
        db.commit()
        db.refresh(existing)
        obj = existing
    else:
        obj = Player(
            discord_id=player.discord_id,
            primary_handle=player.primary_handle,
            display_name=player.display_name,
            avatar_url=player.avatar_url,
            is_npc=player.is_npc,
            aliases=aliases_json,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)

    return PlayerOut(
        id=obj.id,
        discord_id=obj.discord_id,
        primary_handle=obj.primary_handle,
        display_name=obj.display_name,
        avatar_url=obj.avatar_url,
        is_npc=obj.is_npc,
        aliases=json.loads(obj.aliases or "[]"),
    )


@app.get("/players/{discord_id}", response_model=PlayerOut)
async def get_player(
    discord_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    """
    Fetch a single player by their Discord user ID.
    """
    obj = (
        db.query(Player)
        .filter(Player.discord_id == discord_id)
        .first()
    )
    if not obj:
        raise HTTPException(status_code=404, detail="Player not found")

    return PlayerOut(
        id=obj.id,
        discord_id=obj.discord_id,
        primary_handle=obj.primary_handle,
        display_name=obj.display_name,
        avatar_url=obj.avatar_url,
        is_npc=obj.is_npc,
        aliases=json.loads(obj.aliases or "[]"),
    )


@app.get("/players/by_handle/{handle}", response_model=list[PlayerOut])
async def find_players_by_handle(
    handle: str,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    """
    Search for players by handle / display name / alias substring.
    Useful when someone types a name instead of a raw Discord ID.
    """
    term = f"%{handle}%"

    objs = (
        db.query(Player)
        .filter(
            or_(
                Player.primary_handle.ilike(term),
                Player.display_name.ilike(term),
                Player.aliases.ilike(term),
            )
        )
        .all()
    )

    results: List[PlayerOut] = []
    for obj in objs:
        results.append(
            PlayerOut(
                id=obj.id,
                discord_id=obj.discord_id,
                primary_handle=obj.primary_handle,
                display_name=obj.display_name,
                avatar_url=obj.avatar_url,
                is_npc=obj.is_npc,
                aliases=json.loads(obj.aliases or "[]"),
            )
        )

    return results
