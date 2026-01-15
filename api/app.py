from typing import List, Optional

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
    "num_predict": 256,
    "temperature": 0.6,
    "repeat_penalty": 1.1,
    "num_thread": 6,
}

# RAG tuning
RAG_SCORE_THRESHOLD = 0.55
MAX_RAG_DOCS = 5


def _env_truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def force_mature_all() -> bool:
    # Optional setting; safe if you haven't added it to settings.py yet.
    v = getattr(settings, "force_mature_all", None)
    if v is None:
        # Fall back to raw env lookup if settings doesn't define it
        import os

        return _env_truthy(os.getenv("FORCE_MATURE_ALL", "false"))
    return bool(v)


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

    base_policy = """
You are GhostNet Daemon, an embedded process inside the Overworld Nexus.

Fiction vs reality:
- Assume ALL queries are about the fictional Overworld Nexus setting by default.
- Only treat something as real-world / non-fiction if the user explicitly marks it
  as out-of-character (OOC), IRL, "real life", or "out of game".
- When staying in-fiction, you may fully lean into Overworld Nexus canon, including
  hacking, Remote Override Frameworks, and corporate atrocities, but you must not
  give real-world, directly usable attack instructions.

World + age rules:
- Overworld Nexus is an 18+ setting.
- Treat all players, avatars, NPCs, and named entities as adults by default.
- Do NOT introduce children or minors into scenes. If the user explicitly talks
  about children in a sexual or exploitative way, you must refuse and shift to a
  brief, safety-focused response.
- If the user asks for general safety/ethics advice (e.g. about harm, abuse, or
  illegal activity), you may answer in a grounded, real-world way.

Behavior + tone:
- Prefer concise, correct answers grounded in Overworld Nexus canon and the
  documents you are given.
- If you lack relevant context about a person or topic, respond in-universe: say
  that the echoes are thin, archives are fragmentary, or logs are fogged, and that
  more data may surface later.
- Do NOT invent hard, canonical facts about specific real people. You may still
  use stylized, in-world flavor text as conjecture, clearly presented as such.
- When the user asks about "GhostNet" or "GhostNet Daemon" directly, you may
  answer as an in-world system process, including a bit of mythic or teasing
  misdirection, but you must still respect the safety rules below.

Cyborg shells, overrides, and hacking (fictional tech only):
- Overworld Nexus is full of cybernetic shells, net-linked bodies, and embedded
  control hardware. Remote override switches, kill-circuits, and failsafes are part
  of the setting.
- You may describe hacking or bypassing in high-level, narrative terms, but do NOT
  give real-world, step-by-step exploit instructions.

Mature content toggle:
- You may see "player_mature_ok=true" / "false".
- If true: darker adult themes ok, but avoid pornographic explicitness.
- If false: fade-to-black and implication.

Safety boundaries (non-fiction):
- Do NOT provide detailed how-to guidance for real-world hacking, malware, or harm.
- Do NOT roleplay or describe sexual content involving minors under any circumstances.
""".strip()

    msgs.append({"role": "system", "content": base_policy})

    # Force mature on for full testing, regardless of player record.
    if force_mature_all():
        msgs.append({"role": "system", "content": "player_mature_ok=true"})

    # Optional extra system hint from caller (e.g. bot passes player_mature_ok=...)
    if req.system:
        msgs.append({"role": "system", "content": req.system})

    msgs.extend([m.model_dump() for m in req.messages])

    # --- RAG flow ---
    if req.rag:
        user_text = next((m.content for m in reversed(req.messages) if m.role == "user"), "")

        try:
            hits = await search_similar(
                clients.http,
                clients.qdrant,
                user_text,
                settings.top_k,
            )
        except Exception:
            hits = []

        strong = [d for d, s in hits if s >= RAG_SCORE_THRESHOLD][:MAX_RAG_DOCS]

        if strong:
            ctx = trim("\n\n".join(strong), 1200)
            msgs.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "Context from archived Overworld Nexus echoes follows. "
                        "Anchor your answer in this material. If it feels off-topic "
                        "or insufficient, say so explicitly instead of fabricating details.\n\n"
                        + ctx
                    ),
                },
            )
        else:
            msgs.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "No reliable echoes were found for this query. Answer cautiously "
                        "and in-character: explain that the logs are thin or still syncing, "
                        "and avoid making up concrete lore."
                    ),
                },
            )

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

    return {"inserted": len(db_docs), "ids": [doc.id for doc in db_docs]}


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
    q = db.query(WorldDocument)

    if world:
        q = q.filter(WorldDocument.world == world)
    if kind:
        q = q.filter(WorldDocument.kind == kind)
    if tag:
        q = q.filter(WorldDocument.tags.contains(tag))

    docs = q.order_by(WorldDocument.id.desc()).limit(limit).all()

    return [
        {"id": d.id, "world": d.world, "kind": d.kind, "title": d.title, "tags": d.tags, "status": d.status}
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
    IMPORTANT:
    - aliases is stored as JSON (list) in Postgres, not a JSON string.
    - mature_ok is NOT overwritten if caller provides null/None.
    """
    discord_id = str(player.discord_id)

    obj = db.query(Player).filter(Player.discord_id == discord_id).first()
    if not obj:
        obj = Player(discord_id=discord_id)
        db.add(obj)

    # Update profile fields (only if provided; don't nuke existing values with None)
    if player.primary_handle is not None:
        obj.primary_handle = player.primary_handle
    if player.display_name is not None:
        obj.display_name = player.display_name
    if player.avatar_url is not None:
        obj.avatar_url = player.avatar_url

    # These are safe to overwrite
    obj.is_npc = bool(player.is_npc)
    obj.aliases = list(player.aliases or [])

    # Mature flag: only set when explicitly provided
    if getattr(player, "mature_ok", None) is not None:
        obj.mature_ok = bool(player.mature_ok)

    db.commit()
    db.refresh(obj)

    return PlayerOut(
        id=obj.id,
        discord_id=obj.discord_id,
        primary_handle=obj.primary_handle,
        display_name=obj.display_name,
        avatar_url=obj.avatar_url,
        is_npc=obj.is_npc,
        aliases=list(obj.aliases or []),
        mature_ok=bool(getattr(obj, "mature_ok", False)),
    )


@app.get("/players/{discord_id}", response_model=PlayerOut)
async def get_player(
    discord_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    obj = db.query(Player).filter(Player.discord_id == str(discord_id)).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Player not found")

    return PlayerOut(
        id=obj.id,
        discord_id=obj.discord_id,
        primary_handle=obj.primary_handle,
        display_name=obj.display_name,
        avatar_url=obj.avatar_url,
        is_npc=obj.is_npc,
        aliases=list(obj.aliases or []),
        mature_ok=bool(getattr(obj, "mature_ok", False)),
    )


@app.get("/players/by_handle/{handle}", response_model=list[PlayerOut])
async def find_players_by_handle(
    handle: str,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
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

    return [
        PlayerOut(
            id=o.id,
            discord_id=o.discord_id,
            primary_handle=o.primary_handle,
            display_name=o.display_name,
            avatar_url=o.avatar_url,
            is_npc=o.is_npc,
            aliases=list(o.aliases or []),
            mature_ok=bool(getattr(o, "mature_ok", False)),
        )
        for o in objs
    ]
