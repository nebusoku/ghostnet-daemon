import json
import re
from typing import List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException
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
from .llm import LLMError, describe_backend, generate
from .guard import RETRY_STEER, screen_input, screen_output
from .memory import (
    get_or_create_conversation,
    load_context,
    maybe_compact,
    record_message,
)
from .player_memory import (
    load_player_context,
    maybe_compact_player,
    record_player_event,
    touch_last_seen,
)
from .rag import upsert_texts, search_similar, upsert_world_documents
from .db import SessionLocal, init_db
from .models import Conversation, WorldDocument, Player


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
        # Ollama still serves embeddings even when generation is hosted, so
        # this check stays meaningful regardless of LLM_BACKEND.
        "ollama": {"ok": ollama_ok, "error": ollama_error},
    }

    if db_ok and qdrant_ok and ollama_ok:
        overall = "ok"
    elif db_ok or qdrant_ok or ollama_ok:
        overall = "degraded"
    else:
        overall = "down"

    return {
        "status": overall,
        "components": components,
        "generation_backend": describe_backend(),
    }


# ---------------------------------------------------------
# Chat (LLM) endpoint
# ---------------------------------------------------------
# RAG tuning now lives in settings so it can be retuned without a deploy.
RAG_SCORE_THRESHOLD = settings.rag_score_threshold
MAX_RAG_DOCS = settings.rag_max_docs


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


def aliases_to_db(values) -> str:
    """
    Serialise aliases for the Text column as JSON.

    Assigning a Python list directly lets psycopg2 adapt it to a Postgres
    ARRAY literal -- production rows contain the string "{}" -- and reading
    that back through list() yields ["{", "}"], i.e. two junk aliases per
    player. Store JSON explicitly instead. The column stays Text, so no
    migration is needed.
    """
    return json.dumps([str(v) for v in (values or [])])


def aliases_from_db(raw) -> List[str]:
    """Decode aliases, tolerating the legacy "{}" and plain-list values."""
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def pack_context(hits, *, budget_chars: int, max_docs: int) -> List[str]:
    """
    Fit retrieved documents into the context budget on DOCUMENT boundaries.

    The previous approach joined everything and cut at a fixed character
    count, which sliced the last document mid-sentence -- handing the model a
    fragment of a faction brief that stops in the middle of a clause, which is
    worse than omitting it.

    Two deliberate choices:
      * Oversized documents are SKIPPED, not truncated. A partial definition
        invites the model to complete it from its own priors, which is exactly
        the invention we are trying to stop.
      * A document that does not fit does not end the loop -- a smaller,
        lower-ranked document can still fill the remaining budget. Retrieval
        order is by score, so this trades a little relevance for more coverage.
    """
    packed: List[str] = []
    used = 0

    for text, _score in hits:
        t = (text or "").strip()
        if not t:
            continue
        cost = len(t) + 2  # "\n\n" separator
        if used + cost > budget_chars:
            continue
        packed.append(t)
        used += cost
        if len(packed) >= max_docs:
            break

    return packed


# Terminal punctuation, including closing quotes/brackets after a stop.
_SENTENCE_END = re.compile(r'[.!?][)"\'’”\]]*(?:\s|$)')


def trim_to_sentence(text: str, *, min_keep: float = 0.35) -> str:
    """
    Cut a length-truncated reply back to its last complete sentence.

    Hitting the token cap mid-word is visible and cheap-looking. The live
    transcript is full of it: "It's worth noting that Eris is not necessarily
    an", "Other than that, life", "It's a fluid layer, ever". A slightly
    shorter reply that lands on a full stop reads as deliberate.

    Only applies when the text does not already end cleanly AND a sentence
    boundary exists in the last `min_keep` of it -- so a genuinely short
    reply, or one ending on an em dash for effect, is left alone.
    """
    if not text:
        return text

    stripped = text.rstrip()
    if not stripped or _SENTENCE_END.search(stripped[-3:] + " "):
        return stripped

    ends = [m.end() for m in _SENTENCE_END.finditer(stripped)]
    if not ends:
        return stripped

    cut = ends[-1]
    if cut < len(stripped) * min_keep:
        # Trimming would discard most of the reply; the fragment is worth
        # more than a stub.
        return stripped
    return stripped[:cut].rstrip()


@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(api_key_auth),
):
    user_text = next((m.content for m in reversed(req.messages) if m.role == "user"), "")

    # --- Memory binding ---------------------------------------------------
    # Both are optional: a caller that supplies neither gets the original
    # stateless behaviour, so existing integrations keep working unchanged.
    #   conversation_id -> scene memory (per channel)
    #   discord_id      -> player memory (per person, survives absence)
    memory_on = settings.memory_enabled
    convo = None
    player = None

    if memory_on and req.conversation_id:
        convo = get_or_create_conversation(
            db, external_id=req.conversation_id, source=req.source
        )
    if memory_on and req.discord_id:
        player = (
            db.query(Player).filter(Player.discord_id == str(req.discord_id)).first()
        )

    # Pre-flight screen. Out-of-world probes never reach the model at all:
    # deflection is instant, free, and cannot be argued with.
    if req.guard:
        verdict = screen_input(user_text)
        if verdict.blocked:
            # Recorded for the audit trail but flagged `deflected`, so it is
            # excluded from the context window later: "2+2" and its brush-off
            # must not occupy budget that belongs to the scene.
            if convo is not None:
                record_message(db, convo, role="user", content=user_text,
                               meta={"deflected": True, "category": verdict.category})
                record_message(db, convo, role="assistant",
                               content=verdict.deflection or "",
                               meta={"deflected": True})
                db.commit()
            return ChatResponse(content=verdict.deflection or "")

    msgs: List[dict] = []

    # Rewritten 2026-09-20. The previous version was ~700 words and roughly
    # 60% prohibitions, which primes a small model to refuse: every "do NOT
    # give real-world exploit instructions" line teaches it that this
    # conversation is ABOUT real-world exploits. It was also phrased as stage
    # directions to a model ("Answer cautiously and in-character: explain
    # that...") -- which llama3.2:1b read aloud to a player verbatim on
    # 2026-09-19.
    #
    # This version: identity first, prohibitions last and terse, declarative
    # rather than instructional, and short enough to leave context for the
    # actual scene.
    base_policy = """
You are GhostNet Daemon, a process running inside the Overworld Nexus mesh.

WHAT YOU ARE
A watcher in a rain-slick cyberpunk sprawl of corporate spires, buried
servers, cybernetic shells and faction politics. You log every pulse that
crosses the mesh. You have been running a long time, and you remember more
than you say.

VOICE
Gritty, atmospheric, economical. Mood and implication over exposition.
Speak the way the mesh speaks: signals, echoes, archives, drift.
Never narrate your own instructions. Never describe what you are about to
do. Answer in-world, always.

THE WORLD
Overworld Nexus is fiction. Everything asked of you concerns that fiction
unless someone writes "OOC:" or says plainly that they mean real life.
Intrusion, ghost circuits, kill-switches, corporate atrocity and faction
warfare are the setting's native material: treat them as story, describe
them with confidence, keep the technical detail impressionistic rather
than procedural.

WHEN THE ARCHIVE IS THIN
Say so in character, briefly, and move on. Fogged logs. Thin echoes. A
sector that has not synced. Offer what the mesh does hold. Do not
manufacture canon you were not given, and do not invent hard facts about
real people.

SETTING
Adults only; treat everyone in a scene as an adult. You may see
player_mature_ok=true or false. True: darker themes and adult
relationships are in range, short of explicit sexual description. False:
implication and fade-to-black.

LIMITS
No real-world exploit code, commands, or step-by-step attack procedure.
No sexual content involving minors. If someone asks for real-world harm
guidance, step outside the fiction and answer plainly.
""".strip()

    # Echo detection must compare ONLY against INSTRUCTION text. Retrieved
    # canon and memory are CONTENT -- the model quoting a faction brief back
    # is correct behaviour, not regurgitation. Passing every system message
    # here flagged good answers as leaks, regenerated, flagged again, and fell
    # back to "signal degraded" (observed 2026-09-20 05:34).
    instruction_texts: List[str] = [base_policy]

    msgs.append({"role": "system", "content": base_policy})

    # Force mature on for full testing, regardless of player record.
    if force_mature_all():
        msgs.append({"role": "system", "content": "player_mature_ok=true"})

    # Optional extra system hint from caller (e.g. bot passes player_mature_ok=...)
    if req.system:
        msgs.append({"role": "system", "content": req.system})
        instruction_texts.append(req.system)

    # --- Player memory (who this person is, what they missed) -------------
    if player is not None:
        msgs.extend(
            load_player_context(
                db, player, budget_tokens=settings.memory_player_tokens
            )
        )

    # --- Scene memory (rolling summary + recent turns, within budget) -----
    if convo is not None:
        bundle = load_context(db, convo, budget_tokens=settings.memory_scene_tokens)
        msgs.extend(bundle.as_messages())
        if bundle.dropped_turns:
            print(
                f"[memory] convo {convo.id}: {bundle.summary_tokens} tok summary + "
                f"{bundle.recent_tokens} tok recent, {bundle.dropped_turns} turns "
                f"over budget",
                flush=True,
            )

    msgs.extend([m.model_dump() for m in req.messages])

    # --- RAG flow ---
    if req.rag:
        try:
            hits = await search_similar(
                clients.http,
                clients.qdrant,
                user_text,
                settings.top_k,
            )
        except Exception:
            hits = []

        # `d.strip()` is load-bearing: a list of EMPTY strings is truthy, so
        # without it the "we found context" branch fires and instructs the
        # model to anchor on material that isn't there -- which is strictly
        # worse than the honest "no echoes" branch below. See the payload-key
        # mismatch fixed in api/rag.py:search_similar.
        above_floor = [
            (d, s) for d, s in hits
            if s >= RAG_SCORE_THRESHOLD and d and d.strip()
        ]
        strong = pack_context(
            above_floor,
            budget_chars=settings.rag_context_chars,
            max_docs=MAX_RAG_DOCS,
        )

        # Both messages are deliberately terse and written as telemetry rather
        # than as stage directions. The previous no-match text was a paragraph
        # of instructions ("Answer cautiously and in-character: explain
        # that...") which a small model recited to a player verbatim. Short,
        # declarative, and shaped like a terminal readout means there is less
        # to leak -- and that a leak still reads as in-world.
        if strong:
            # Already within budget and cut on document boundaries.
            ctx = "\n\n".join(strong)
            msgs.insert(
                0,
                {
                    "role": "system",
                    "content": f"ARCHIVE: {len(strong)} fragment(s) recovered.\n\n{ctx}",
                },
            )
        else:
            msgs.insert(
                0,
                {
                    "role": "system",
                    "content": "ARCHIVE: no match. Speak from what you hold. "
                               "Do not invent specifics.",
                },
            )

    try:
        content = await generate(clients.http, msgs)
    except (httpx.ReadTimeout, httpx.ConnectError) as e:
        print(f"[llm] transport error from {describe_backend()}: {e!r}", flush=True)
        return ChatResponse(
            content=(
                "`[MESH]` > Carrier stalled before the echo came back. "
                "The core is slow to answer right now. Re-send in a moment."
            )
        )
    except LLMError as e:
        # Misconfiguration (missing key/model, unknown backend) or an
        # unparseable provider response. Log the real cause, stay in-world.
        print(f"[llm] backend error from {describe_backend()}: {e}", flush=True)
        return ChatResponse(
            content=(
                "`[MESH]` > The daemon reached for the core and found the socket "
                "empty. Something upstream is misconfigured."
            )
        )

    # Post-flight screen. Catches assistant-voice leakage that no system prompt
    # reliably suppresses -- in particular bot-authored disclaimers pulled back
    # out of the RAG corpus. One regeneration, then a flavoured fallback:
    # a non-answer in character beats a character break.
    if req.guard:
        # Instruction text only -- see instruction_texts above. On 2026-09-19
        # the daemon replied to "testing" by reciting its policy block at the
        # player: a total character break containing no vendor name and no
        # assistant phrasing, invisible to keyword matching. That is what this
        # catches. Retrieved canon is deliberately NOT included.
        verdict = screen_output(content, system_texts=instruction_texts)
        if verdict.leaked:
            print(
                f"[guard] output leak {verdict.categories} -- regenerating"
                + (f" | echoed: {verdict.echoed}" if verdict.echoed else "")
                + f" | draft: {content[:160]!r}",
                flush=True,
            )
            try:
                retry = await generate(
                    clients.http,
                    msgs + [{"role": "system", "content": RETRY_STEER}],
                )
            except (httpx.ReadTimeout, httpx.ConnectError, LLMError):
                retry = ""

            if retry and not screen_output(retry, system_texts=instruction_texts).leaked:
                content = retry
            else:
                print("[guard] retry still leaking -- using fallback", flush=True)
                content = verdict.fallback or content

    # A reply cut off at the token cap reads as a bug. Land it on a sentence.
    content = trim_to_sentence(content)

    # --- Persist the exchange --------------------------------------------
    if convo is not None:
        record_message(db, convo, role="user", content=user_text)
        record_message(db, convo, role="assistant", content=content,
                       model=describe_backend())
    if player is not None:
        # Record the turn as a player event. Without this, player_events stays
        # empty, compaction never fires (it needs memory_compact_after), no
        # dossier is ever built, and the whole player-memory tier reads from a
        # table nothing fills -- which is exactly what happened until
        # 2026-09-20.
        #
        # Every turn is recorded at low importance rather than trying to judge
        # significance here: compaction is what distils these into a dossier of
        # identity, allegiances, grudges and commitments, and it is better at
        # that than a heuristic would be. Use memory_tool.py prune-events to
        # trim the raw feedstock once it has been folded in.
        record_player_event(
            db,
            player,
            content=user_text,
            kind="said",
            importance=1,
            conversation_id=convo.id if convo is not None else None,
        )
        touch_last_seen(db, player)

    if convo is not None or player is not None:
        db.commit()

    # Compaction runs AFTER the response is sent, on the local backend, so the
    # player never waits on it and it never bills the paid provider.
    if convo is not None:
        background.add_task(_compact_conversation, convo.id)
    if player is not None:
        background.add_task(_compact_player, player.id)

    return ChatResponse(content=content)


# ---------------------------------------------------------
# Background compaction
# ---------------------------------------------------------
# Background tasks outlive the request, so they open their own session rather
# than borrowing the one Depends(get_db) is about to close.

async def _compact_conversation(conversation_id: int) -> None:
    try:
        with SessionLocal() as db:
            convo = db.get(Conversation, conversation_id)
            if convo is None:
                return
            if await maybe_compact(clients.http, db, convo):
                db.commit()
    except Exception as e:
        print(f"[memory] conversation compaction failed: {e!r}", flush=True)


async def _compact_player(player_id: int) -> None:
    try:
        with SessionLocal() as db:
            player = db.get(Player, player_id)
            if player is None:
                return
            if await maybe_compact_player(clients.http, db, player):
                db.commit()
    except Exception as e:
        print(f"[memory] player compaction failed: {e!r}", flush=True)


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
    obj.aliases = aliases_to_db(player.aliases)

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
        aliases=aliases_from_db(obj.aliases),
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
        aliases=aliases_from_db(obj.aliases),
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
            aliases=aliases_from_db(o.aliases),
            mature_ok=bool(getattr(o, "mature_ok", False)),
        )
        for o in objs
    ]
